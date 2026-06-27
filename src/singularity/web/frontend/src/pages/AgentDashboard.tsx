import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Plus, Trash2, Edit3, Wrench, Star, Power, PowerOff, GripVertical } from 'lucide-react'

const LEVELS = ['D', 'E+', 'E']
const TYPES = ['openai-agent', 'claude-cli', 'zhipu-api', 'anthropic_api']

export default function AgentDashboard() {
  const [agents, setAgents] = useState<any>({})
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState<{level:string;model:string}|null>(null)
  const [form, setForm] = useState<any>({level:'E', model:'', type:'openai-agent', entry:'', api_key_env:'', max_turns:5, roles:[], default:false, sandbox:'worktree'})
  const [selectedSkills, setSelectedSkills] = useState<{level:string;model:string;skills:string[];available:string[]}|null>(null)

  const [disabledMap, setDisabledMap] = useState<Record<string,string[]>>({})

  const fetch = () => { setLoading(true); api.agents().then(d => {
    setDisabledMap(d?._disabled || {})
    setAgents(d)
  }).finally(()=>setLoading(false)) }
  useEffect(() => { fetch() }, [])

  const handleSave = async () => {
    if (!form.model.trim()) return
    if (editing) {
      await api.updateAgent(editing.level, editing.model, form)
    } else {
      await api.addAgent(form)
    }
    setShowForm(false); setEditing(null); fetch()
  }

  const handleEdit = (level: string, a: any) => {
    setEditing({level, model: a.model})
    setForm({...a, level})
    setShowForm(true)
  }

  const handleNew = (level: string) => {
    setEditing(null)
    setForm({level, model:'', type:'openai-agent', entry:'', api_key_env:'', max_turns:5, roles:[], default:false, sandbox:'worktree'})
    setShowForm(true)
  }

  const handleSkills = async (level: string, model: string) => {
    try {
      const d = await api.agentSkills(level, model)
      setSelectedSkills({level, model, skills: d.skills || [], available: d.available || []})
    } catch { setSelectedSkills({level, model, skills: [], available: []}) }
  }

  const handleSaveSkills = async () => {
    if (!selectedSkills) return
    await api.updateAgentSkills(selectedSkills.level, selectedSkills.model, selectedSkills.skills)
    setSelectedSkills(null); fetch()
  }

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:14}}>Agent 管理</h2>

      {/* Add/Edit Form Modal */}
      {showForm && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--accent)',borderRadius:'var(--radius)',padding:16,marginBottom:16}}>
          <div style={{fontSize:14,fontWeight:600,marginBottom:10}}>{editing ? `编辑 ${editing.model}` : '添加 Agent'}</div>
          <div style={{display:'grid',gridTemplateColumns:'1fr 1fr 1fr',gap:8}}>
            <div><label style={lbl}>层级</label><select value={form.level} onChange={e=>setForm({...form,level:e.target.value})} style={inp}>{LEVELS.map(l=><option key={l}>{l}</option>)}</select></div>
            <div><label style={lbl}>模型名</label><input value={form.model} onChange={e=>setForm({...form,model:e.target.value})} style={inp} placeholder="gpt-5.5"/></div>
            <div><label style={lbl}>类型</label><select value={form.type} onChange={e=>setForm({...form,type:e.target.value})} style={inp}>{TYPES.map(t=><option key={t}>{t}</option>)}</select></div>
            <div><label style={lbl}>Entry URL</label><input value={form.entry||''} onChange={e=>setForm({...form,entry:e.target.value})} style={inp} placeholder="https://api.openai.com/v1"/></div>
            <div><label style={lbl}>API Key 环境变量</label><input value={form.api_key_env||''} onChange={e=>setForm({...form,api_key_env:e.target.value})} style={inp} placeholder="OPENAI_API_KEY"/></div>
            <div><label style={lbl}>Max Turns</label><input type="number" value={form.max_turns||5} onChange={e=>setForm({...form,max_turns:parseInt(e.target.value)})} style={inp}/></div>
          </div>
          <div style={{display:'flex',gap:12,marginTop:8,alignItems:'center'}}>
            <label style={{fontSize:12,color:'var(--text-secondary)',display:'flex',alignItems:'center',gap:4}}>
              <input type="checkbox" checked={form.default||false} onChange={e=>setForm({...form,default:e.target.checked})}/> 默认</label>
            <label style={{fontSize:12,color:'var(--text-secondary)'}}>Sandbox: <select value={form.sandbox||'worktree'} onChange={e=>setForm({...form,sandbox:e.target.value})} style={{...inp,width:120}}>{['worktree','none'].map(s=><option key={s}>{s}</option>)}</select></label>
            <input value={(form.roles||[]).join(',')} onChange={e=>setForm({...form,roles:e.target.value.split(',').map((s:string)=>s.trim()).filter(Boolean)})} style={{...inp,flex:1}} placeholder="角色标签: architecture,daily"/>
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
            <button onClick={handleSaveSkills} style={btn('var(--accent-green)')}>保存</button>
            <button onClick={()=>setSelectedSkills(null)} style={btn('var(--bg-tertiary)')}>关闭</button>
          </div>
        </div>
      )}

      {/* Agent Grid */}
      {LEVELS.map(lvl=>{
        const list = agents?.[lvl] || []
        return (
          <div key={lvl} style={{marginBottom:16}}>
            <div style={{display:'flex',alignItems:'center',marginBottom:8}}>
              <span style={{fontSize:14,fontWeight:600,color:'var(--text-secondary)'}}>{lvl} 层 ({list.length})</span>
              <button onClick={()=>handleNew(lvl)} style={{marginLeft:'auto',background:'var(--accent)',color:'#fff',border:'none',borderRadius:4,padding:'4px 10px',cursor:'pointer',fontSize:11,display:'flex',alignItems:'center',gap:3}}><Plus size={12}/> 添加</button>
            </div>
            <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(300px,1fr))',gap:10}}>
              {list.map((a:any)=>(
                <div key={a.model} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
                  <div style={{display:'flex',justifyContent:'space-between',alignItems:'start'}}>
                    <div style={{flex:1}}>
                      <div style={{display:'flex',alignItems:'center',gap:6}}>
                        <span style={{fontWeight:600,fontFamily:'var(--font-mono)',fontSize:13}}>{a.model}</span>
                        {a.default && <Star size={12} color='var(--accent-yellow)'/>}
                      </div>
                      <div style={{fontSize:11,color:'var(--text-secondary)',marginTop:2}}>{a.type} · max {a.max_turns||5} turns</div>
                    </div>
                    <div style={{display:'flex',gap:2}}>
                      <button onClick={()=>handleEdit(lvl,a)} title="编辑" style={iconBtn}><Edit3 size={14}/></button>
                      <button onClick={()=>handleSkills(lvl,a.model)} title="Skills" style={iconBtn}><Wrench size={14}/></button>
                      <button onClick={()=>api.deleteAgent(lvl,a.model).then(fetch)} title="删除" style={{...iconBtn,color:'var(--accent-red)'}}><Trash2 size={14}/></button>
                    </div>
                  </div>
                  {a.roles && a.roles.length>0 && <div style={{display:'flex',gap:4,marginTop:6,flexWrap:'wrap'}}>{a.roles.map((r:string)=><span key={r} style={{background:'var(--bg-tertiary)',color:'var(--text-secondary)',padding:'2px 6px',borderRadius:3,fontSize:10}}>{r}</span>)}</div>}
                  <div style={{fontSize:10,color:'var(--text-muted)',marginTop:6,fontFamily:'var(--font-mono)',wordBreak:'break-all'}}>{a.entry?.slice(0,80)||'无 entry'}</div>
                </div>
              ))}
            </div>
            {/* Disabled agents */}
            {(disabledMap[lvl]||[]).length > 0 && <div style={{marginTop:10,fontSize:11,color:'var(--text-muted)'}}>
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
