import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Plus, Trash2, Edit3, Wrench, Globe, Terminal, Link } from 'lucide-react'

const LEVELS = ['D', 'E+', 'E']

export default function AgentDashboard() {
  const [agents, setAgents] = useState<any>({})
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState<{level:string;model:string}|null>(null)
  const [form, setForm] = useState<any>({level:'E', model:'', type:'openai-agent', entry:'', api_key_env:'', max_turns:5, roles:[], default:false})
  const [selectedSkills, setSelectedSkills] = useState<{level:string;model:string;skills:string[];available:string[]}|null>(null)
  const [disabledMap, setDisabledMap] = useState<Record<string,string[]>>({})

  const fetch = () => { setLoading(true); api.agents().then(d => {
    setDisabledMap(d?._disabled || {}); setAgents(d)
  }).finally(()=>setLoading(false)) }
  useEffect(() => { fetch() }, [])

  const isCLI = (a: any) => a.type === 'claude-cli'

  // Infer API key env from model name
  const inferKey = (model: string) => {
    const m = model.toLowerCase()
    if (m.includes('gpt') || m.includes('openai')) return 'OPENAI_API_KEY'
    if (m.includes('deepseek')) return 'DEEPSEEK_API_KEY'
    if (m.includes('kimi') || m.includes('moonshot')) return 'KIMI_API_KEY'
    if (m.includes('glm') || m.includes('zhipu')) return 'ZHIPU_API_KEY'
    if (m.includes('qwen') || m.includes('dashscope')) return 'DASHSCOPE_API_KEY'
    if (m.includes('claude') || m.includes('anthropic')) return 'ANTHROPIC_API_KEY'
    return ''
  }

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  // Group agents by endpoint (entry URL), deduplicate
  const groupByEndpoint = (list: any[]) => {
    const groups: Record<string, any[]> = {}
    for (const a of list) {
      // Infer endpoint label: prefer explicit entry, fallback to api_key hint
      let key: string
      if (isCLI(a)) {
        key = `cli:${a.model}`
      } else if (a.entry) {
        key = a.entry
      } else {
        // Infer from api_key_env or model name
        const env = a.api_key_env || ''
        if (env.includes('OPENAI')) key = 'OpenAI API'
        else if (env.includes('DEEPSEEK')) key = 'DeepSeek API'
        else if (env.includes('KIMI') || env.includes('MOONSHOT')) key = 'Kimi API'
        else if (env.includes('ZHIPU') || env.includes('GLM')) key = '智谱 API'
        else if (env.includes('DASHSCOPE') || env.includes('QWEN')) key = '通义千问 API'
        else if (env.includes('ANTHROPIC')) key = 'Anthropic API'
        else key = `默认连接 (${env||a.model})`
      }
      if (!groups[key]) groups[key] = []
      groups[key].push(a)
    }
    return groups
  }

  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:4}}>API 连接</h2>
      <div style={{fontSize:12,color:'var(--text-muted)',marginBottom:14}}>
        管理 API 端点和本地 CLI 工具的连接配置。模型在 <a href="/models" style={{color:'var(--accent)'}}>模型管理</a> 中维护，此处指定每个端点上使用哪个模型。
      </div>

      {showForm && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--accent)',borderRadius:'var(--radius)',padding:16,marginBottom:16}}>
          <div style={{fontSize:14,fontWeight:600,marginBottom:10}}>{editing ? '编辑连接' : '添加连接'}</div>
          <div style={{marginBottom:10}}>
            <label style={lbl}>连接方式</label>
            <div style={{display:'flex',gap:8}}>
              {[{v:'openai-agent',l:'API 端点',icon:Globe},{v:'claude-cli',l:'CLI 工具',icon:Terminal}].map(t=>(
                <label key={t.v} onClick={()=>setForm({...form,type:t.v})} style={{
                  background:form.type===t.v?'var(--accent)':'var(--bg-tertiary)',color:form.type===t.v?'#fff':'var(--text-secondary)',
                  border:'1px solid var(--border)',borderRadius:4,padding:'6px 12px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:6}}>
                  <t.icon size={14}/>{t.l}</label>
              ))}
            </div>
          </div>
          <div style={{display:'grid',gridTemplateColumns:'1fr 1fr 1fr',gap:8}}>
            {isCLI(form) ? (
              <>
                <div><label style={lbl}>工具名</label><input value={form.model} onChange={e=>setForm({...form,model:e.target.value})} style={inp} placeholder="codex / claude"/></div>
                <div style={{gridColumn:'span 2'}}><label style={lbl}>CLI 命令</label><input value={form.entry||''} onChange={e=>setForm({...form,entry:e.target.value})} style={inp} placeholder="/opt/homebrew/bin/codex exec -p {prompt}"/></div>
              </>
            ) : (
              <>
                <div><label style={lbl}>API 端点</label><input value={form.entry||''} onChange={e=>setForm({...form,entry:e.target.value})} style={inp} placeholder="https://api.openai.com/v1"/></div>
                <div><label style={lbl}>使用模型</label><input value={form.model} onChange={e=>setForm({...form,model:e.target.value})} style={inp} placeholder="gpt-5.5"/></div>
                <div><label style={lbl}>环境变量</label><input value={form.api_key_env||''} onChange={e=>setForm({...form,api_key_env:e.target.value})} style={inp} placeholder="OPENAI_API_KEY"/></div>
              </>
            )}
            <div><label style={lbl}>层级</label><select value={form.level} onChange={e=>setForm({...form,level:e.target.value})} style={inp}>{LEVELS.map(l=><option key={l}>{l}</option>)}</select></div>
            <div><label style={lbl}>Max Turns</label><input type="number" value={form.max_turns||5} onChange={e=>setForm({...form,max_turns:parseInt(e.target.value)})} style={inp}/></div>
          </div>
          <div style={{display:'flex',gap:8,marginTop:10,alignItems:'center'}}>
            <label style={{fontSize:12,color:'var(--text-secondary)',display:'flex',alignItems:'center',gap:4}}><input type="checkbox" checked={form.default||false} onChange={e=>setForm({...form,default:e.target.checked})}/> 默认</label>
            <input value={(form.roles||[]).join(',')} onChange={e=>setForm({...form,roles:e.target.value.split(',').map((s:string)=>s.trim()).filter(Boolean)})} style={{...inp,flex:1}} placeholder="角色标签: architecture,daily"/>
            <button onClick={async()=>{if(!form.model.trim())return;editing?await api.updateAgent(editing.level,editing.model,form):await api.addAgent(form);setShowForm(false);setEditing(null);fetch()}} style={btn('var(--accent-green)')}>保存</button>
            <button onClick={()=>{setShowForm(false);setEditing(null)}} style={btn('var(--bg-tertiary)')}>取消</button>
          </div>
        </div>
      )}

      {selectedSkills && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--accent-purple)',borderRadius:'var(--radius)',padding:16,marginBottom:16}}>
          <div style={{fontSize:14,fontWeight:600,marginBottom:8}}>Skills — {selectedSkills.model}</div>
          <div style={{display:'flex',flexWrap:'wrap',gap:6,marginBottom:10}}>
            {selectedSkills.skills.length===0 && <span style={{fontSize:11,color:'var(--text-muted)'}}>未分配</span>}
            {selectedSkills.skills.map((s:string)=><span key={s} onClick={()=>setSelectedSkills({...selectedSkills,skills:selectedSkills.skills.filter((x:string)=>x!==s)})} style={{background:'var(--accent-purple)',color:'#fff',padding:'4px 8px',borderRadius:4,fontSize:11,cursor:'pointer'}}>{s} ✕</span>)}
          </div>
          <div style={{fontSize:11,color:'var(--text-muted)',marginBottom:4}}>可用:</div>
          <div style={{display:'flex',flexWrap:'wrap',gap:6}}>
            {(selectedSkills.available||[]).filter((s:string)=>!selectedSkills.skills.includes(s)).map((s:string)=><span key={s} onClick={()=>setSelectedSkills({...selectedSkills,skills:[...selectedSkills.skills,s]})} style={{background:'var(--bg-tertiary)',padding:'4px 8px',borderRadius:4,fontSize:11,cursor:'pointer',color:'var(--text-secondary)'}}>+ {s}</span>)}
          </div>
          <div style={{display:'flex',gap:8,marginTop:10}}>
            <button onClick={async()=>{if(selectedSkills){await api.updateAgentSkills(selectedSkills.level,selectedSkills.model,selectedSkills.skills);setSelectedSkills(null);fetch()}}} style={btn('var(--accent-green)')}>保存</button>
            <button onClick={()=>setSelectedSkills(null)} style={btn('var(--bg-tertiary)')}>关闭</button>
          </div>
        </div>
      )}

      {LEVELS.map(lvl=>{
        const list = agents?.[lvl] || []
        const groups = groupByEndpoint(list)
        return (
          <div key={lvl} style={{marginBottom:20}}>
            <div style={{display:'flex',alignItems:'center',marginBottom:10}}>
              <span style={{fontSize:14,fontWeight:600,color:'var(--text-secondary)'}}>{lvl} 层 — {list.length} 个连接</span>
              <button onClick={()=>{setEditing(null);setForm({level:lvl,model:'',type:'openai-agent',entry:'',api_key_env:'',max_turns:5,roles:[],default:false});setShowForm(true)}}
                style={{marginLeft:'auto',background:'var(--accent)',color:'#fff',border:'none',borderRadius:4,padding:'4px 10px',cursor:'pointer',fontSize:11,display:'flex',alignItems:'center',gap:3}}><Plus size={12}/> 添加连接</button>
            </div>

            {Object.entries(groups).map(([endpoint, agents_]) => {
              const first = agents_[0]
              const isCLIGroup = isCLI(first)
              return (
                <div key={endpoint} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14,marginBottom:8}}>
                  {/* Endpoint header */}
                  <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:agents_.length>1?10:0}}>
                    <div style={{display:'flex',alignItems:'center',gap:8}}>
                      {isCLIGroup ? <Terminal size={16} color='var(--accent-green)'/> : <Globe size={16} color='var(--accent)'/>}
                      <div>
                        <div style={{fontWeight:600,fontSize:13,fontFamily:'var(--font-mono)',color:'var(--text-primary)'}}>
                          {isCLIGroup ? first.model : (first.entry || '默认端点')}
                        </div>
                        <div style={{fontSize:10,color:'var(--text-muted)',marginTop:1}}>
                          {isCLIGroup ? `CLI 工具 · ${first.entry?.slice(0,60)||'?'}` : `API 端点${first.api_key_env ? ' · 🔑'+first.api_key_env : ''}`}
                        </div>
                      </div>
                    </div>
                    {first.default && <span style={{fontSize:10,color:'var(--accent-yellow)'}}>默认连接</span>}
                  </div>

                  {/* Models on this endpoint */}
                  <div style={{display:'flex',flexWrap:'wrap',gap:6,alignItems:'center',marginLeft:24}}>
                    <span style={{fontSize:10,color:'var(--text-muted)',marginRight:4}}>模型:</span>
                    {agents_.map((a:any)=>(
                      <span key={a.model} style={{
                        display:'inline-flex',alignItems:'center',gap:6,
                        background:'var(--bg-tertiary)',border:'1px solid var(--border)',
                        borderRadius:4,padding:'3px 8px',fontSize:11,
                      }}>
                        <span style={{fontFamily:'var(--font-mono)',color:'var(--accent)',fontWeight:600}}>{a.model}</span>
                        {a.roles && a.roles.length>0 && <span style={{color:'var(--text-muted)',fontSize:9}}>{a.roles.join(',')}</span>}
                        <span style={{display:'flex',gap:2}}>
                          <button onClick={()=>{setEditing({level:lvl,model:a.model});setForm({...a,level:lvl});setShowForm(true)}} title="编辑" style={iconBtn}><Edit3 size={10}/></button>
                          <button onClick={async()=>{try{const d=await api.agentSkills(lvl,a.model);setSelectedSkills({level:lvl,model:a.model,skills:d.skills||[],available:d.available||[]})}catch(e){setSelectedSkills({level:lvl,model:a.model,skills:[],available:[]})}}} title="Skills" style={iconBtn}><Wrench size={10}/></button>
                          <button onClick={()=>api.deleteAgent(lvl,a.model).then(fetch)} title="移除" style={{...iconBtn,color:'var(--accent-red)'}}><Trash2 size={10}/></button>
                        </span>
                      </span>
                    ))}
                    {/* Add model to this endpoint */}
                    {!isCLIGroup && (
                      <button onClick={()=>{setEditing(null);setForm({level:lvl,model:'',type:'openai-agent',entry:first.entry,api_key_env:first.api_key_env||'',max_turns:first.max_turns||5,roles:[],default:false});setShowForm(true)}}
                        style={{background:'none',border:'1px dashed var(--border)',borderRadius:4,padding:'3px 8px',cursor:'pointer',color:'var(--text-muted)',fontSize:11,display:'flex',alignItems:'center',gap:2}}>
                        <Plus size={10}/> 加模型</button>
                    )}
                  </div>
                </div>
              )
            })}

            {(disabledMap[lvl]||[]).length > 0 && <div style={{marginTop:8,fontSize:11,color:'var(--text-muted)'}}>
              已禁用: {(disabledMap[lvl]||[]).map((m:string)=><span key={m} style={{display:'inline-flex',alignItems:'center',gap:4,background:'var(--bg-tertiary)',color:'var(--text-muted)',padding:'2px 8px',borderRadius:3,margin:'2px 4px',fontFamily:'var(--font-mono)',fontSize:10}}>{m} <button onClick={()=>api.addAgent({level:lvl,model:m,type:'openai-agent',max_turns:5,roles:[],sandbox:'worktree',api_key_env:inferKey(m)}).then(fetch)} style={{background:'none',border:'none',color:'var(--accent-green)',cursor:'pointer',fontSize:10,padding:0}}>启用</button></span>)}
            </div>}
          </div>
        )
      })}
    </div>
  )
}

const lbl = { fontSize:10, color:'var(--text-muted)', display:'block', marginBottom:2 } as const
const inp = { width:'100%', background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:4,padding:'5px 8px',color:'var(--text-primary)',fontSize:12 } as const
const btn = (bg:string) => ({ background:bg, color:'#fff', border:'none', borderRadius:4, padding:'6px 14px', cursor:'pointer', fontSize:12 })
const iconBtn = { background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:1 } as const
