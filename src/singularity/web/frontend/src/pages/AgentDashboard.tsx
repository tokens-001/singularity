import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Bot, Edit3, Wrench, Check, X, User, Circle, FileText } from 'lucide-react'

interface RoleInfo {
  key: string; name: string; level: string; description: string; persona: string
  model: string; available_models: string[]; system_prompt: string
  tasks: { running: number; pending: number; done: number; failed: number; total: number }
  skills: string[]
}
interface PersonaInfo { key: string; name: string; description: string; style_prompt: string; voice: string }

export default function AgentDashboard() {
  const [roles, setRoles] = useState<RoleInfo[]>([])
  const [personas, setPersonas] = useState<Record<string,PersonaInfo>>({})
  const [agents, setAgents] = useState<any>({})
  const [loading, setLoading] = useState(true)
  const [editingModel, setEditingModel] = useState<string | null>(null)
  const [editingPersona, setEditingPersona] = useState<string | null>(null)
  const [editModel, setEditModel] = useState('')
  const [skillModal, setSkillModal] = useState<{role:string;model:string;skills:string[];available:string[]}|null>(null)
  const [promptModal, setPromptModal] = useState<RoleInfo | null>(null)

  const fetch = async () => {
    setLoading(true)
    const [agtData, taskData, roleData, skillData] = await Promise.all([api.agents(), api.tasks(), api.roles(), api.skills()])
    setAgents(agtData); setPersonas(roleData?.personas || {})
    const allSkills = skillData.map((s:any)=>s.name||s)

    const taskCounts: Record<string,{running:number;pending:number;done:number;failed:number}> = {}
    for (const t of taskData) { const r=t.route_role||'';if(!taskCounts[r])taskCounts[r]={running:0,pending:0,done:0,failed:0};const s=t.status;if(s==='running')taskCounts[r].running++;else if(s==='pending')taskCounts[r].pending++;else if(s==='done')taskCounts[r].done++;else if(s==='failed')taskCounts[r].failed++ }

    const rd = roleData?.roles || {}
    const result: RoleInfo[] = Object.values(rd).map((r: any) => {
      const level = r.level || 'E'; const list = agtData?.[level] || []
      const match = list.find((a:any)=>a.default&&(a.roles||[]).includes(r.key))
      const any = list.find((a:any)=>(a.roles||[]).includes(r.key))
      const model = match?.model||any?.model||list.find((a:any)=>a.default)?.model||list[0]?.model||'无'
      const tc = taskCounts[r.key]||{running:0,pending:0,done:0,failed:0}
      const available_models = list.filter((a:any)=>a.type!=='claude-cli').map((a:any)=>a.model)
      return { key:r.key, name:r.name, level, description:r.description, persona:r.persona||'', model, available_models, system_prompt:r.system_prompt||'', tasks:{...tc,total:tc.running+tc.pending+tc.done+tc.failed}, skills:[] }
    })

    // Load skills per role
    for (const r of result) {
      try { const d = await api.agentSkills(r.level, r.model); r.skills = d.skills||[] } catch { r.skills = [] }
    }

    setRoles(result); setLoading(false)
  }
  useEffect(() => { fetch() }, [])
  useEffect(() => { const t=setInterval(fetch,15000);return ()=>clearInterval(t) }, [])

  const handleSwitchModel = async (roleKey: string, newModel: string) => {
    const role = roles.find(r=>r.key===roleKey); if(!role) return
    for (const lvl of ['D','E+','E']) { for (const a of (agents?.[lvl]||[])) {
      if (a.model===newModel) { const nr=[...new Set([...(a.roles||[]),roleKey])];await api.updateAgent(lvl,a.model,{roles:nr}) }
      if (a.model===role.model&&a.model!==newModel) { await api.updateAgent(lvl,a.model,{roles:(a.roles||[]).filter((x:string)=>x!==roleKey)}) }
    }}
    setEditingModel(null); fetch()
  }

  const handleSkills = async (roleKey: string, model: string) => {
    try { const d=await api.agentSkills(roles.find(r=>r.key===roleKey)?.level||'E',model);setSkillModal({role:roleKey,model,skills:d.skills||[],available:d.available||[]}) }
    catch { setSkillModal({role:roleKey,model,skills:[],available:[]}) }
  }

  const levelColor = (l:string) => l==='D'?'#f0883e':l==='E+'?'#a371f7':'#58a6ff'
  const totalActive = roles.reduce((s,r)=>s+r.tasks.running+r.tasks.pending,0)

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  return (
    <div>
      <div style={{display:'flex',alignItems:'center',marginBottom:14}}>
        <h2 style={{fontSize:16,fontWeight:600}}>智能体</h2>
        <span style={{marginLeft:8,fontSize:12,color:'var(--text-muted)'}}>{roles.length} 个角色 · {totalActive>0?<span style={{color:'var(--accent)'}}>{totalActive} 个活跃</span>:'空闲'}</span>
      </div>

      {skillModal && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--accent-purple)',borderRadius:'var(--radius)',padding:16,marginBottom:16}}>
          <div style={{fontSize:14,fontWeight:600,marginBottom:8}}>{roles.find(r=>r.key===skillModal!.role)?.name} — Skill 分配</div>
          <div style={{display:'flex',flexWrap:'wrap',gap:6,marginBottom:10}}>
            {skillModal.skills.length===0&&<span style={{fontSize:11,color:'var(--text-muted)'}}>未分配</span>}
            {skillModal.skills.map((s:string)=><span key={s} onClick={()=>setSkillModal({...skillModal,skills:skillModal.skills.filter((x:string)=>x!==s)})} style={{background:'var(--accent-purple)',color:'#fff',padding:'4px 8px',borderRadius:4,fontSize:11,cursor:'pointer'}}>{s} ✕</span>)}
          </div>
          <div style={{fontSize:11,color:'var(--text-muted)',marginBottom:4}}>可用:</div>
          <div style={{display:'flex',flexWrap:'wrap',gap:6}}>
            {(skillModal.available||[]).filter((s:string)=>!skillModal.skills.includes(s)).map((s:string)=><span key={s} onClick={()=>setSkillModal({...skillModal,skills:[...skillModal.skills,s]})} style={{background:'var(--bg-tertiary)',padding:'4px 8px',borderRadius:4,fontSize:11,cursor:'pointer',color:'var(--text-secondary)'}}>+ {s}</span>)}
          </div>
          <div style={{display:'flex',gap:8,marginTop:10}}>
            <button onClick={async()=>{if(skillModal){const r=roles.find(x=>x.key===skillModal.role);await api.updateAgentSkills(r?.level||'E',skillModal.model,skillModal.skills);setSkillModal(null);fetch()}}} style={btn('var(--accent-green)')}>保存</button>
            <button onClick={()=>setSkillModal(null)} style={btn('var(--bg-tertiary)')}>关闭</button>
          </div>
        </div>
      )}

      {promptModal && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:16,marginBottom:16}}>
          <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:8}}>
            <span style={{fontWeight:600,fontSize:14}}>{promptModal.name} — 系统提示词</span>
            <button onClick={()=>setPromptModal(null)} style={iconBtn}>✕</button>
          </div>
          <pre style={{fontSize:10,color:'var(--text-secondary)',whiteSpace:'pre-wrap',maxHeight:300,overflow:'auto',fontFamily:'var(--font-mono)',background:'var(--bg-primary)',padding:10,borderRadius:4}}>{promptModal.system_prompt||'(空)'}</pre>
        </div>
      )}

      <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',overflow:'hidden'}}>
        <table style={{width:'100%',borderCollapse:'collapse'}}>
          <thead><tr style={{borderBottom:'1px solid var(--border)',fontSize:11,color:'var(--text-muted)',textAlign:'left'}}>
            <th style={{padding:'8px 10px',width:30}}></th>
            <th style={{padding:'8px 10px',width:140}}>角色</th>
            <th style={{padding:'8px 10px',width:100}}>人格</th>
            <th style={{padding:'8px 10px',width:130}}>模型</th>
            <th style={{padding:'8px 10px',width:140}}>Skill</th>
            <th style={{padding:'8px 10px',width:80}}>活动</th>
          </tr></thead>
          <tbody>
            {roles.map(r=>{
              const isBusy = r.tasks.running>0||r.tasks.pending>0
              const p = personas[r.persona]
              return (
                <tr key={r.key} style={{borderBottom:'1px solid var(--border)',fontSize:12,background:isBusy?'rgba(88,166,255,0.05)':'transparent'}}>
                  <td style={{padding:'8px 6px',textAlign:'center'}}>
                    <Circle size={10} fill={isBusy?'var(--accent)':'transparent'} color={isBusy?'var(--accent)':'var(--border)'}/>
                  </td>
                  <td style={{padding:'8px 10px'}}>
                    <div style={{display:'flex',alignItems:'center',gap:6}}>
                      <span style={{fontWeight:600}}>{r.name}</span>
                      <span style={{background:levelColor(r.level)+'22',color:levelColor(r.level),padding:'1px 5px',borderRadius:3,fontSize:9,fontWeight:600}}>{r.level}</span>
                    </div>
                    <div style={{fontSize:10,color:'var(--text-muted)',marginTop:1}}>{r.key} · {r.description.slice(0,24)}</div>
                  </td>
                  <td style={{padding:'8px 10px'}} onClick={e=>e.stopPropagation()}>
                    {editingPersona===r.key ? (
                      <div style={{display:'flex',gap:4,alignItems:'center'}}>
                        <select value={r.persona} onChange={async(e)=>{const v=e.target.value;await api.updateRole(r.key,{persona:v});setEditingPersona(null);fetch()}} style={inp}>
                          {Object.entries(personas).map(([pk,pv])=><option key={pk} value={pk}>{pv.name}</option>)}
                        </select>
                        <button onClick={()=>setEditingPersona(null)} style={iconBtn}><X size={12}/></button>
                      </div>
                    ) : (
                      <span onClick={()=>setEditingPersona(r.key)} style={{cursor:'pointer',display:'flex',alignItems:'center',gap:4,fontSize:11}}>
                        <User size={12} color={p?'var(--accent-purple)':'var(--text-muted)'}/>
                        {p?.name || r.persona || '—'}
                        <Edit3 size={10} color='var(--text-muted)'/>
                      </span>
                    )}
                  </td>
                  <td style={{padding:'8px 10px'}} onClick={e=>e.stopPropagation()}>
                    {editingModel===r.key?(
                      <div style={{display:'flex',gap:4,alignItems:'center'}}>
                        <select value={editModel} onChange={e=>setEditModel(e.target.value)} style={inp}>
                          {r.available_models.map((m:string)=><option key={m} value={m}>{m}</option>)}</select>
                        <button onClick={()=>handleSwitchModel(r.key,editModel)} style={{...iconBtn,color:'var(--accent-green)'}}><Check size={12}/></button>
                        <button onClick={()=>setEditingModel(null)} style={iconBtn}><X size={12}/></button>
                      </div>
                    ):(
                      <span onClick={()=>{setEditingModel(r.key);setEditModel(r.model)}} style={{fontFamily:'var(--font-mono)',fontSize:11,color:'var(--accent)',cursor:'pointer'}}>{r.model}</span>
                    )}
                  </td>
                  <td style={{padding:'8px 10px'}} onClick={e=>e.stopPropagation()}>
                    <div style={{display:'flex',alignItems:'center',gap:4}}>
                      <button onClick={()=>handleSkills(r.key,r.model)} title="Skill" style={iconBtn}><Wrench size={14}/></button>
                      <div style={{display:'flex',gap:2,flexWrap:'wrap',maxWidth:100}}>
                        {r.skills.slice(0,2).map((s:string)=><span key={s} style={{background:'var(--bg-tertiary)',padding:'1px 5px',borderRadius:2,fontSize:9,color:'var(--accent-purple)'}}>{s}</span>)}
                        {r.skills.length>2&&<span style={{fontSize:9,color:'var(--text-muted)'}}>+{r.skills.length-2}</span>}
                      </div>
                    </div>
                  </td>
                  <td style={{padding:'8px 10px'}}>
                    <div style={{display:'flex',gap:6,fontSize:11}}>
                      {r.tasks.running>0&&<span style={{color:'var(--accent)'}}>⏳{r.tasks.running}</span>}
                      {r.tasks.pending>0&&<span style={{color:'var(--text-muted)'}}>待{r.tasks.pending}</span>}
                      {r.tasks.done>0&&<span style={{color:'var(--accent-green)'}}>✓{r.tasks.done}</span>}
                      {r.tasks.failed>0&&<span style={{color:'var(--accent-red)'}}>✗{r.tasks.failed}</span>}
                      {r.tasks.total===0&&<span style={{color:'var(--text-muted)'}}>—</span>}
                    </div>
                    <button onClick={(e)=>{e.stopPropagation();setPromptModal(r)}} style={{...iconBtn,marginTop:2}}><FileText size={10}/> 提示词</button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

const inp = { background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:3,padding:'2px 4px',color:'var(--text-primary)',fontSize:11,width:100 } as const
const btn = (bg:string) => ({ background:bg, color:'#fff', border:'none', borderRadius:4, padding:'6px 14px', cursor:'pointer', fontSize:12 })
const iconBtn = { background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2 } as const
