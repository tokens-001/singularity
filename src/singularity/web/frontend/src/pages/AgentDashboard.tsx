import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Plus, Trash2, Edit3, Wrench, Terminal, Globe, Cpu } from 'lucide-react'

const LEVELS = ['D', 'E+', 'E']

const TYPE_INFO: Record<string,{icon:any;label:string;desc:string}> = {
  'openai-agent': {icon:Globe,label:'OpenAI 兼容 API',desc:'直接调模型 API，支持 OpenAI/DeepSeek/Kimi/GLM/Qwen'},
  'claude-cli': {icon:Terminal,label:'本地 CLI 工具',desc:'调本地命令行 AI 工具（Claude Code/Codex 等）'},
  'anthropic_api': {icon:Cpu,label:'Anthropic 原生 API',desc:'Anthropic 专有 API 格式'},
  'zhipu-api': {icon:Cpu,label:'智谱专有 API',desc:'智谱 GLM 原生 API'},
}

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

  const handleSave = async () => {
    if (!form.model.trim()) return
    if (editing) { await api.updateAgent(editing.level, editing.model, form) }
    else { await api.addAgent(form) }
    setShowForm(false); setEditing(null); fetch()
  }

  const isCLI = (a: any) => a.type === 'claude-cli'

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:4}}>Agent 运行时</h2>
      <div style={{fontSize:12,color:'var(--text-muted)',marginBottom:14}}>管理任务执行方式：API 端点 或 本地 CLI 工具。模型在 <a href="/models" style={{color:'var(--accent)'}}>模型管理</a> 配置。</div>

      {/* Add Form */}
      {showForm && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--accent)',borderRadius:'var(--radius)',padding:16,marginBottom:16}}>
          <div style={{fontSize:14,fontWeight:600,marginBottom:10}}>{editing ? '编辑运行时' : '添加运行时'}</div>
          <div style={{marginBottom:10}}>
            <label style={lbl}>类型</label>
            <div style={{display:'flex',gap:8,flexWrap:'wrap'}}>
              {Object.entries(TYPE_INFO).map(([k,v])=>(
                <label key={k} onClick={()=>setForm({...form,type:k})} style={{
                  background:form.type===k?'var(--accent)':'var(--bg-tertiary)',color:form.type===k?'#fff':'var(--text-secondary)',
                  border:'1px solid var(--border)',borderRadius:4,padding:'6px 10px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:6}}>
                  <v.icon size={14}/>{v.label}<span style={{fontSize:10,opacity:0.7}}>— {v.desc.slice(0,30)}</span>
                </label>
              ))}
            </div>
          </div>
          <div style={{display:'grid',gridTemplateColumns:'1fr 1fr 1fr',gap:8}}>
            <div><label style={lbl}>{isCLI(form)?'工具名':'模型名'}</label><input value={form.model} onChange={e=>setForm({...form,model:e.target.value})} style={inp} placeholder={isCLI(form)?'codex / claude':'gpt-5.5'}/></div>
            <div><label style={lbl}>层级</label><select value={form.level} onChange={e=>setForm({...form,level:e.target.value})} style={inp}>{LEVELS.map(l=><option key={l}>{l}</option>)}</select></div>
            <div><label style={lbl}>Max Turns</label><input type="number" value={form.max_turns||5} onChange={e=>setForm({...form,max_turns:parseInt(e.target.value)})} style={inp}/></div>
            <div><label style={lbl}>{isCLI(form)?'CLI 命令':'API 地址'}</label><input value={form.entry||''} onChange={e=>setForm({...form,entry:e.target.value})} style={inp} placeholder={isCLI(form)?'/path/to/tool -p {prompt}':'https://api.openai.com/v1'}/></div>
            <div><label style={lbl}>环境变量</label><input value={form.api_key_env||''} onChange={e=>setForm({...form,api_key_env:e.target.value})} style={inp} placeholder="OPENAI_API_KEY"/></div>
          </div>
          <div style={{display:'flex',gap:8,marginTop:10}}>
            <label style={{fontSize:12,color:'var(--text-secondary)',display:'flex',alignItems:'center',gap:4}}><input type="checkbox" checked={form.default||false} onChange={e=>setForm({...form,default:e.target.checked})}/> 设为默认</label>
            <input value={(form.roles||[]).join(',')} onChange={e=>setForm({...form,roles:e.target.value.split(',').map((s:string)=>s.trim()).filter(Boolean)})} style={{...inp,flex:1}} placeholder="角色: architecture,daily"/>
          </div>
          <div style={{display:'flex',gap:8,marginTop:10}}>
            <button onClick={handleSave} style={btn('var(--accent-green)')}>保存</button>
            <button onClick={()=>{setShowForm(false);setEditing(null)}} style={btn('var(--bg-tertiary)')}>取消</button>
          </div>
        </div>
      )}

      {/* Skills Modal */}
      {selectedSkills && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--accent-purple)',borderRadius:'var(--radius)',padding:16,marginBottom:16}}>
          <div style={{fontSize:14,fontWeight:600,marginBottom:8}}>Skills — {selectedSkills.model}</div>
          <div style={{fontSize:11,color:'var(--text-muted)',marginBottom:4}}>已分配:</div>
          <div style={{display:'flex',flexWrap:'wrap',gap:6,marginBottom:10}}>
            {selectedSkills.skills.length===0 && <span style={{fontSize:11,color:'var(--text-muted)'}}>无</span>}
            {selectedSkills.skills.map((s:string)=><span key={s} onClick={()=>setSelectedSkills({...selectedSkills,skills:selectedSkills.skills.filter((x:string)=>x!==s)})} style={{background:'var(--accent-purple)',color:'#fff',padding:'4px 8px',borderRadius:4,fontSize:11,cursor:'pointer'}}>{s} ✕</span>)}
          </div>
          <div style={{fontSize:11,color:'var(--text-muted)',marginBottom:4}}>可用 (点击添加):</div>
          <div style={{display:'flex',flexWrap:'wrap',gap:6}}>
            {(selectedSkills.available||[]).filter((s:string)=>!selectedSkills.skills.includes(s)).map((s:string)=><span key={s} onClick={()=>setSelectedSkills({...selectedSkills,skills:[...selectedSkills.skills,s]})} style={{background:'var(--bg-tertiary)',padding:'4px 8px',borderRadius:4,fontSize:11,cursor:'pointer',color:'var(--text-secondary)'}}>+ {s}</span>)}
          </div>
          <div style={{display:'flex',gap:8,marginTop:10}}>
            <button onClick={async()=>{if(selectedSkills){await api.updateAgentSkills(selectedSkills.level,selectedSkills.model,selectedSkills.skills);setSelectedSkills(null);fetch()}}} style={btn('var(--accent-green)')}>保存</button>
            <button onClick={()=>setSelectedSkills(null)} style={btn('var(--bg-tertiary)')}>关闭</button>
          </div>
        </div>
      )}

      {/* Agent List — grouped by type, not model */}
      {LEVELS.map(lvl=>{
        const list = agents?.[lvl] || []
        // Group by type
        const byType: Record<string,any[]> = {}
        for (const a of list) {
          const t = a.type || 'openai-agent'
          if (!byType[t]) byType[t] = []
          byType[t].push(a)
        }
        return (
          <div key={lvl} style={{marginBottom:20}}>
            <div style={{display:'flex',alignItems:'center',marginBottom:8}}>
              <span style={{fontSize:14,fontWeight:600,color:'var(--text-secondary)'}}>{lvl} 层 — {list.length} 个运行时</span>
              <button onClick={()=>{setEditing(null);setForm({level:lvl,model:'',type:'openai-agent',entry:'',api_key_env:'',max_turns:5,roles:[],default:false});setShowForm(true)}}
                style={{marginLeft:'auto',background:'var(--accent)',color:'#fff',border:'none',borderRadius:4,padding:'4px 10px',cursor:'pointer',fontSize:11,display:'flex',alignItems:'center',gap:3}}><Plus size={12}/> 添加</button>
            </div>

            {Object.entries(byType).map(([typ, agents])=>{
              const info = TYPE_INFO[typ] || {icon:Cpu,label:typ,desc:''}
              return (
                <div key={typ} style={{marginBottom:12}}>
                  <div style={{display:'flex',alignItems:'center',gap:6,marginBottom:6}}>
                    <info.icon size={14} color={typ==='claude-cli'?'var(--accent-green)':'var(--accent)'}/>
                    <span style={{fontSize:12,fontWeight:600,color:'var(--text-primary)'}}>{info.label}</span>
                    <span style={{fontSize:10,color:'var(--text-muted)'}}>— {info.desc}</span>
                  </div>
                  <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(300px,1fr))',gap:8}}>
                    {agents.map((a:any)=>(
                      <div key={a.model} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:'10px 14px'}}>
                        <div style={{display:'flex',justifyContent:'space-between',alignItems:'start'}}>
                          <div style={{flex:1}}>
                            <div style={{fontWeight:600,fontFamily:'var(--font-mono)',fontSize:13}}>
                              {isCLI(a) ? a.model : <><span style={{color:'var(--accent)'}}>{a.model}</span></>}
                              {a.default && <span style={{color:'var(--accent-yellow)',marginLeft:4,fontSize:10}}>默认</span>}
                            </div>
                            <div style={{fontSize:10,color:'var(--text-muted)',marginTop:2}}>
                              {isCLI(a) ? <>命令: {a.entry?.slice(0,50)||'?'}</> : <>端点: {a.entry?.slice(0,50)||'默认'}</>}
                              {a.api_key_env && <span style={{marginLeft:8}}>🔑 {a.api_key_env}</span>}
                            </div>
                          </div>
                          <div style={{display:'flex',gap:2,flexShrink:0}}>
                            <button onClick={()=>{setEditing({level:lvl,model:a.model});setForm({...a,level:lvl});setShowForm(true)}} style={iconBtn}><Edit3 size={14}/></button>
                            <button onClick={async()=>{try{const d=await api.agentSkills(lvl,a.model);setSelectedSkills({level:lvl,model:a.model,skills:d.skills||[],available:d.available||[]})}catch(e){setSelectedSkills({level:lvl,model:a.model,skills:[],available:[]})}}} style={iconBtn}><Wrench size={14}/></button>
                            <button onClick={()=>api.deleteAgent(lvl,a.model).then(fetch)} style={{...iconBtn,color:'var(--accent-red)'}}><Trash2 size={14}/></button>
                          </div>
                        </div>
                        {a.roles && a.roles.length>0 && <div style={{display:'flex',gap:4,marginTop:6,flexWrap:'wrap'}}>{a.roles.map((r:string)=><span key={r} style={{background:'var(--bg-tertiary)',color:'var(--text-secondary)',padding:'2px 6px',borderRadius:3,fontSize:10}}>{r}</span>)}</div>}
                      </div>
                    ))}
                  </div>
                </div>
              )
            })}

            {(disabledMap[lvl]||[]).length > 0 && <div style={{marginTop:8,fontSize:11,color:'var(--text-muted)'}}>
              已禁用: {(disabledMap[lvl]||[]).map((m:string)=><span key={m} style={{display:'inline-flex',alignItems:'center',gap:4,background:'var(--bg-tertiary)',color:'var(--text-muted)',padding:'2px 8px',borderRadius:3,margin:'2px 4px',fontFamily:'var(--font-mono)',fontSize:10}}>{m} <button onClick={()=>api.addAgent({level:lvl,model:m,type:'openai-agent',max_turns:5,roles:[],sandbox:'worktree'}).then(fetch)} style={{background:'none',border:'none',color:'var(--accent-green)',cursor:'pointer',fontSize:10,padding:0}}>启用</button></span>)}
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
const iconBtn = { background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2 } as const
