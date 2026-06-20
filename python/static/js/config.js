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
// 基础设施 Tab — API库 / 模型库 / Agent编组
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
  const html = '<div id="model-edit-'+esc(m.id)+'" style="margin:4px 0;padding:8px;border:1px solid var(--accent);background:var(--bg2)">层级：'+['E','E+','D'].map(t=>{
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
    if (d.error) { body.innerHTML = '<span style="color:var(--st-fail)">'+esc(d.error)+'</span>'; return; }
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
        const tc = {E:'var(--accent)','E+':'var(--st-pending)',D:'var(--st-hold)'}[t]||'var(--text3)';
        return '<span style="color:'+tc+';font-family:var(--mono);font-size:8px">'+t+'</span>';
      }).join('/');
      const dot = m.api_available
        ? '<span style="color:var(--st-done);font-size:7px">●</span>'
        : '<span style="color:var(--text3);font-size:7px">○</span>';
      const cm = costMark[m.cost]||'_';
      let html = '<div style="padding:4px 0 4px 12px;border-bottom:1px solid var(--bg3)">';
      html += '<div style="display:flex;align-items:center;gap:8px;font-size:10px">';
      html += '<span style="width:12px;text-align:center">'+dot+'</span>';
      html += '<span style="font-family:var(--mono);font-weight:500;min-width:80px;color:var(--text)">'+esc(m.display)+'</span>';
      html += '<span style="font-family:var(--mono);font-size:9px;color:var(--text3)">'+esc(m.id)+'</span>';
      html += '<span style="font-size:9px;color:var(--text3);font-family:var(--mono)">'+cm+'</span>';
      html += '<span style="font-size:8px;color:var(--text3)">'+m.speed+'</span>';
      if (m.reasoning) html += '<span style="font-size:8px;color:var(--st-hold);font-family:var(--mono)">RSN</span>';
      html += '<span style="flex:1"></span>';
      html += '<span style="font-size:8px;font-family:var(--mono)">'+tiers+'</span>';
      html += ' <button class="btn sm" style="color:var(--accent);font-size:7px;padding:1px 3px;margin-left:4px" onclick="event.stopPropagation();editModel(\''+esc(m.id)+'\')">✎</button>';
      html += ' <button class="btn sm" style="color:var(--st-fail);font-size:7px;padding:1px 3px;margin-left:4px" onclick="event.stopPropagation();removeModel(\''+esc(m.id)+'\')">DEL</button>';
      html += '</div>';
      html += '<div style="font-size:8px;color:var(--text3);margin-top:2px;font-family:var(--mono)">'+esc(m.notes||'')+'</div>';
      html += '<div style="margin-top:2px;display:flex;gap:2px">';
      for (const t of ['E','E+','D']) {
        const on = (m.tiers||[]).includes(t);
        const tc = {E:'var(--accent)','E+':'var(--st-pending)',D:'var(--st-hold)'}[t]||'var(--text3)';
        html += '<span onclick="event.stopPropagation();updateModel(\''+esc(m.id)+'\',\'tiers\',\''+t+'\')" style="cursor:pointer;font-size:7px;font-family:var(--mono);padding:1px 4px;color:'+(on?tc:'var(--text3)')+';background:'+(on?'var(--bg3)':'transparent')+'">['+t+']</span>';
      }
      html += '<span style="font-size:7px;color:var(--text3);font-family:var(--mono)"> 层级</span>';
      for (const t of ['E','E+','D']) {
        if (!(m.tiers||[]).includes(t)) continue;
        const isDef = m._isDefault && m._isDefault[t];
        html += '<span onclick="event.stopPropagation();setDefaultModel(\''+esc(m.id)+'\',\''+t+'\')" style="cursor:pointer;font-size:7px;font-family:var(--mono);padding:1px 4px;color:'+(isDef?'var(--accent)':'var(--text3)')+';background:'+(isDef?'rgba(57,210,192,.12)':'transparent')+'">'+(isDef?'DEF':'def')+'</span>';
      }
      html += '</div></div>';
      return html;
    }
    let html = '';
    const order = ['deepseek','zhipu','kimi','openai','anthropic','other'];
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
        ? '<span style="color:var(--st-done);font-size:7px">●</span>'
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
  document.getElementById('lineup-status').style.color = 'var(--st-pending)';
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
  document.getElementById('lineup-status').style.color = 'var(--st-done)';
}

async function renderLayerSwitch(){
  const body = document.getElementById('layer-switch-body');
  const [agents, models] = await Promise.all([api('/api/agents'), api('/api/models')]);
  if (agents.error || models.error) { body.innerHTML = '<span style="color:var(--st-fail)">加载失败</span>'; return; }

  const tiers = [
    {id:'E', color:'var(--accent)', label:'E 层', desc:'日常执行 · bugfix · 查询'},
    {id:'E+', color:'var(--st-pending)', label:'E+ 层', desc:'复杂构建 · 多文件 · 新模块'},
    {id:'D', color:'var(--st-hold)', label:'D 层', desc:'架构设计 · 审查 · 方案'},
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
        ? `<span style="color:var(--st-done);font-size:7px">●</span>`
        : `<span style="color:var(--st-fail);font-size:7px">●</span>`;
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
        if (d.kind === 'task' || d.kind === 'system' || d.kind === 'workflow' || d.kind === 'memory' || d.kind === 'turn') {
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
    if(ind) { ind.style.color = 'var(--st-fail)'; ind.textContent = '◇'; }
    if (_reconnectTimer) clearTimeout(_reconnectTimer);
    _reconnectTimer = setTimeout(connectSSE, 5000);
  };
  es.onopen = () => {
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
// Skills 面板
// ═══════════════════════════════════════════════════════
async function renderSkills(){
  const [skills, agents] = await Promise.all([
    fetch('/api/skills').then(r=>r.json()).catch(()=>({skills:[]})),
    fetch('/api/agents').then(r=>r.json()).catch(()=>({}))
  ]);
  const skillList = skills.skills || [];
  // 左侧 skill 列表
  const listBody = document.getElementById('skill-list-body');
  if (!listBody) return;
  if (!skillList.length){ listBody.innerHTML = '<span style="color:var(--text2)">无 Skill</span>'; }
  else {
    listBody.innerHTML = skillList.map(s =>
      `<div class="skill-item">
        <span><span class="name">${esc(s.name)}</span>
          <span class="type-tag ${s.type}">${s.type}</span></span>
        <span>${s.source === 'user' ? '<button class="btn-del" onclick="deleteSkill(\''+esc(s.name)+'\')">×</button>' : ''}</span>
      </div>`
    ).join('');
  }
  // 右侧 agent × skill 矩阵
  renderSkillMatrix(skillList, agents);
}

function renderSkillMatrix(skillList, agents){
  const body = document.getElementById('skill-matrix-body');
  if (!body) return;
  if (!skillList.length){ body.innerHTML = '<span style="color:var(--text2)">无 Skill 可绑定</span>'; return; }

  const levels = ['E', 'E+', 'D'];
  let rows = [];
  // header row
  let hdr = '<tr><th>Agent</th>';
  skillList.forEach(s => { hdr += `<th>${s.name}</th>`; });
  hdr += '</tr>';
  rows.push(hdr);

  levels.forEach(lv => {
    const agentCfgs = (agents[lv] || []);
    agentCfgs.forEach(cfg => {
      const m = cfg.model || '';
      if (!m) return;
      let r = `<tr><td>${lv}/${m}</td>`;
      skillList.forEach(s => {
        r += `<td><input type="checkbox"
          data-level="${lv}" data-model="${m}" data-skill="${s.name}"
          onchange="toggleAgentSkill(this)"></td>`;
      });
      r += '</tr>';
      rows.push(r);
    });
  });

  body.innerHTML = `<table class="matrix-table">${rows.join('')}</table>`;

  // 拉取所有 agent 的 skill 绑定，回填 checkbox
  Promise.all(
    levels.flatMap(lv =>
      (agents[lv]||[]).map(cfg =>
        fetch(`/api/agents/${lv}/${cfg.model}/skills`).then(r=>r.json()).then(d => ({
          level: lv, model: cfg.model, skills: (d.skills||[]).map(s=>s.name)
        })).catch(()=>({level:lv,model:cfg.model,skills:[]}))
      )
    )
  ).then(results => {
    results.forEach(r => {
      (r.skills||[]).forEach(sname => {
        const cb = document.querySelector(`input[data-level="${r.level}"][data-model="${r.model}"][data-skill="${sname}"]`);
        if (cb) cb.checked = true;
      });
    });
  });
}

async function toggleAgentSkill(cb){
  const {level, model, skill} = cb.dataset;
  try{
    // 读当前绑定
    const r = await fetch(`/api/agents/${level}/${model}/skills`);
    const d = await r.json();
    const current = (d.skills||[]).map(s=>s.name);
    const updated = cb.checked ? [...current, skill] : current.filter(n=>n!==skill);
    await fetch(`/api/agents/${level}/${model}/skills`, {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({skills: updated})
    });
  }catch(e){ cb.checked = !cb.checked; }
}

function toggleSkillForm(){
  const f = document.getElementById('skill-form');
  if (f) f.classList.toggle('open');
}

async function createSkill(){
  const name = document.getElementById('skill-new-name').value.trim();
  const desc = document.getElementById('skill-new-desc').value.trim();
  const type = document.getElementById('skill-new-type').value;
  const args = document.getElementById('skill-new-args').value.trim();
  const body = document.getElementById('skill-new-body').value;
  if (!name) return toast('请输入名称', 'error');
  try{
    await fetch('/api/skills', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, description:desc, type, arguments:args, body})
    });
    toggleSkillForm();
    renderSkills();
  }catch(e){ toast('创建失败: '+e, 'error'); }
}

async function deleteSkill(name){
  if (!confirm('删除 Skill: ' + name + '?')) return;
  try{
    await fetch('/api/skills/'+encodeURIComponent(name), {method:'DELETE'});
    renderSkills();
  }catch(e){ toast('删除失败: '+e, 'error'); }
}

// ═══════════════════════════════════════════════════════
// Permission 面板
// ═══════════════════════════════════════════════════════
async function renderPermissions(){
  try{
    const r = await fetch('/api/permissions/profiles');
    const d = await r.json();
    const profiles = d.profiles || [];
    const bindings = d.bindings || {};
    // Profiles list
    const pb = document.getElementById('perm-profiles-body');
    if(pb) pb.innerHTML = profiles.map(p =>
      `<div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid var(--grid)">
        <span>${p.builtin?'🔒 ':''}${esc(p.name)} <span style="color:var(--text3)">${esc(p.description)}</span></span>
        <span style="color:var(--text2)">${p.allowed_tools_count?p.allowed_tools_count+'工具':'全部'}</span>
      </div>`
    ).join('');
    // Profile select
    const ps = document.getElementById('perm-profile-select');
    if(ps) ps.innerHTML = profiles.map(p => `<option value="${p.name}">${p.name}${p.builtin?' (内置)':''}</option>`).join('');
    // Model select
    const ms = document.getElementById('perm-model');
    if(ms){
      const ag = await fetch('/api/agents').then(r=>r.json()).catch(()=>({}));
      const lv = document.getElementById('perm-level')?.value || 'E';
      const models = (ag[lv]||[]).map(a=>a.model).filter(Boolean);
      ms.innerHTML = models.map(m=>`<option value="${m}">${m}</option>`).join('');
      if(models.length) showPermBinding();
    }
    // Store bindings
    window._permBindings = bindings;
  }catch(e){}
}

async function showPermBinding(){
  const lv = document.getElementById('perm-level')?.value || 'E';
  const m = document.getElementById('perm-model')?.value || '';
  const info = document.getElementById('perm-binding-info');
  if(!info || !m) return;
  const key = `${lv}/${m}`;
  const profile = (window._permBindings||{})[key] || 'full-access';
  info.textContent = `${key} → ${profile}`;
}

async function bindPerm(){
  const lv = document.getElementById('perm-level')?.value || 'E';
  const m = document.getElementById('perm-model')?.value;
  const p = document.getElementById('perm-profile-select')?.value;
  if(!m||!p) return;
  try{
    await fetch('/api/permissions/bindings', {method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({level:lv, model:m, profile:p})});
    renderPermissions();
  }catch(e){ toast('绑定失败: '+e, 'error'); }
}

// ═══════════════════════════════════════════════════════
// Approval 确认
// ═══════════════════════════════════════════════════════
let _approvalPending = null; // {task_id, action, resolve}
function showApproval(task_id, action, detail){
  const m = document.getElementById('approval-modal');
  const b = document.getElementById('approval-body');
  if (!m || !b) return;
  b.textContent = `任务 ${task_id.slice(0,8)}\n操作: ${action}\n${detail}`;
  m.style.display = 'flex';
  return new Promise((resolve) => {
    _approvalPending = {task_id, action, resolve};
  });
}
function respondApproval(decision){
  const m = document.getElementById('approval-modal');
  if (m) m.style.display = 'none';
  if (_approvalPending) {
    _approvalPending.resolve(decision);
    // 通知后端
    if (_approvalPending.task_id) {
      fetch(`/api/tasks/${_approvalPending.task_id}/approval`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({decision, action: _approvalPending.action})
      }).catch(()=>{});
    }
    _approvalPending = null;
  }
}
function closeApproval(){ respondApproval('reject'); }

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

// ═══════════════════════════════════════════════════════
// Fusion 多模型融合
// ═══════════════════════════════════════════════════════

function updateFusionStatus() {
  const auto = document.getElementById('fusion-auto');
  const hint = document.getElementById('fusion-auto-hint');
  if (auto && hint) {
    hint.textContent = auto.checked
      ? '架构/安全/跨模块任务自动走 Fusion'
      : '需手动指定 route_type=fusion 才走';
  }
}

async function testFusion() {
  const ma = document.getElementById('fusion-model-a').value;
  const mb = document.getElementById('fusion-model-b').value;
  const status = document.getElementById('fusion-status');
  status.textContent = '提交中...';
  status.style.color = 'var(--text2)';
  try {
    const r = await api('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        description: '分析奇点调度平台的架构优缺点，给出改进建议（Fusion 测试，不修改文件）',
        priority: 80,
        route_level: 'E',
        route_type: 'fusion',
      }),
    });
    if (r.ok) {
      status.textContent = `✅ 任务 ${r.task_id.slice(-8)} 已提交 (${ma}+${mb})`;
      status.style.color = '#57d9a3';
      refreshAll();
    } else {
      status.textContent = `❌ ${r.error || '失败'}`;
      status.style.color = '#f44747';
    }
  } catch(e) {
    status.textContent = `❌ ${e.message}`;
    status.style.color = '#f44747';
  }
}

// ═══════════════════════════════════════════════════════
// Init (removed — see app.js)
