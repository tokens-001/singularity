import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Bot, Edit3, Wrench, Check, X, User, Circle, FileText } from 'lucide-react'

interface RoleInfo {
  key: string; name: string; level: string; description: string; persona: string
  model: string; available_models: string[]; system_prompt: string
  tasks: { running: number; pending: number; done: number; failed: number; total: number }
}

interface PersonaInfo { key: string; name: string; description: string; style_prompt: string; voice: string }

export default function AgentDashboard() {
  const [roles, setRoles] = useState<RoleInfo[]>([])
  const [personas, setPersonas] = useState<Record<string,PersonaInfo>>({})
  const [agents, setAgents] = useState<any>({})
  const [loading, setLoading] = useState(true)
  const [editingRole, setEditingRole] = useState<string | null>(null)
  const [editingPersona, setEditingPersona] = useState<string | null>(null)
  const [editModel, setEditModel] = useState('')
  const [skillModal, setSkillModal] = useState<{role:string;model:string;skills:string[];available:string[]}|null>(null)
  const [promptModal, setPromptModal] = useState<RoleInfo | null>(null)

  const fetch = async () => {
    setLoading(true)
    const [agtData, taskData, roleData] = await Promise.all([api.agents(), api.tasks(), api.roles()])
    setAgents(agtData)
    setPersonas(roleData?.personas || {})

    const taskCounts: Record<string, {running:number;pending:number;done:number;failed:number}> = {}
    for (const t of taskData) {
      const r = t.route_role || ''
      const s = t.status
      if (!taskCounts[r]) taskCounts[r] = { running: 0, pending: 0, done: 0, failed: 0 }
      if (s === 'running') taskCounts[r].running++
      else if (s === 'pending') taskCounts[r].pending++
      else if (s === 'done') taskCounts[r].done++
      else if (s === 'failed') taskCounts[r].failed++
    }

    const rd = roleData?.roles || {}
    const systemRoles: RoleInfo[] = Object.values(rd).map((r: any) => {
      const level = r.level || 'E'
      const list = agtData?.[level] || []
      const match = list.find((a:any) => a.default && (a.roles||[]).includes(r.key))
      const any = list.find((a:any) => (a.roles||[]).includes(r.key))
      const model = match?.model || any?.model || list.find((a:any)=>a.default)?.model || list[0]?.model || '无'
      const tc = taskCounts[r.key] || { running:0,pending:0,done:0,failed:0 }
      return {
        key: r.key, name: r.name, level, description: r.description,
        persona: r.persona || '', model,
        available_models: list.map((a:any) => a.model),
        system_prompt: r.system_prompt || '',
        tasks: { ...tc, total: tc.running+tc.pending+tc.done+tc.failed },
      }
    })

    setRoles(systemRoles)
    setLoading(false)
  }

  useEffect(() => { fetch() }, [])
  useEffect(() => { const t = setInterval(fetch, 15000); return () => clearInterval(t) }, [])

  const handleSwitchModel = async (roleKey: string, newModel: string) => {
    const role = roles.find(r => r.key === roleKey); if (!role) return
    for (const lvl of ['D','E+','E']) {
      for (const a of (agents?.[lvl] || [])) {
        if (a.model === newModel) {
          const newRoles = [...new Set([...(a.roles||[]), roleKey])]
          await api.updateAgent(lvl, a.model, { roles: newRoles })
        }
        if (a.model === role.model && a.model !== newModel) {
          const newRoles = (a.roles||[]).filter((r:string) => r !== roleKey)
          await api.updateAgent(lvl, a.model, { roles: newRoles })
        }
      }
    }
    setEditingRole(null); fetch()
  }

  const handleSkills = async (roleKey: string, model: string) => {
    try { const d = await api.agentSkills(roles.find(r=>r.key===roleKey)?.level||'E', model)
      setSkillModal({role:roleKey, model, skills: d.skills||[], available: d.available||[]})
    } catch { setSkillModal({role:roleKey, model, skills:[], available:[]}) }
  }

  const levelColor = (l: string) => l==='D'?'#f0883e':l==='E+'?'#a371f7':'#58a6ff'
  const totalActive = roles.reduce((s,r)=>s+r.tasks.running+r.tasks.pending,0)

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  return (
    <div>
      <div style={{display:'flex',alignItems:'center',marginBottom:14}}>
        <h2 style={{fontSize:16,fontWeight:600}}>智能体</h2>
        <span style={{marginLeft:8,fontSize:12,color:'var(--text-muted)'}}>
          {roles.length} 个角色 · {totalActive>0?<span style={{color:'var(--accent)'}}>{totalActive} 个活跃任务</span>:'空闲'}</span>
      </div>

      {/* Skill Modal */}
      {skillModal && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--accent-purple)',borderRadius:'var(--radius)',padding:16,marginBottom:16}}>
          <div style={{fontSize:14,fontWeight:600,marginBottom:8}}>Skills — {roles.find(r=>r.key===skillModal!.role)?.name} · {skillModal.model}</div>
          <div style={{display:'flex',flexWrap:'wrap',gap:6,marginBottom:10}}>
            {skillModal.skills.length===0 && <span style={{fontSize:11,color:'var(--text-muted)'}}>未分配</span>}
            {skillModal.skills.map((s:string)=><span key={s} onClick={()=>setSkillModal({...skillModal,skills:skillModal.skills.filter((x:string)=>x!==s)})} style={{background:'var(--accent-purple)',color:'#fff',padding:'4px 8px',borderRadius:4,fontSize:11,cursor:'pointer'}}>{s} ✕</span>)}
          </div>
          <div style={{display:'flex',alignItems:'center',marginBottom:4}}>
            <span style={{fontSize:11,color:'var(--text-muted)'}}>可用:</span>
            <button onClick={async()=>{const n=prompt('新 Skill 名称:');if(n){await api.addSkill({name:n,description:'',type:'prompt',content:''});const d=await api.skills();setSkillModal({...skillModal,available:d.map((s:any)=>s.name||s)})}}}
              style={{marginLeft:'auto',background:'none',border:'none',color:'var(--accent)',cursor:'pointer',fontSize:10}}>+ 新建 Skill</button>
          </div>
          <div style={{display:'flex',flexWrap:'wrap',gap:6}}>
            {(skillModal.available||[]).filter((s:string)=>!skillModal.skills.includes(s)).map((s:string)=><span key={s} onClick={()=>setSkillModal({...skillModal,skills:[...skillModal.skills,s]})} style={{background:'var(--bg-tertiary)',padding:'4px 8px',borderRadius:4,fontSize:11,cursor:'pointer',color:'var(--text-secondary)'}}>+ {s} <button onClick={async(e)=>{e.stopPropagation();await api.deleteSkill(s);const d=await api.skills();setSkillModal({...skillModal,available:d.map((x:any)=>x.name||x),skills:skillModal.skills.filter((x:string)=>x!==s)})}} style={{background:'none',border:'none',color:'var(--accent-red)',cursor:'pointer',fontSize:10,padding:0,marginLeft:2}}>✕</button></span>)}
          </div>
          <div style={{display:'flex',gap:8,marginTop:10}}>
            <button onClick={async()=>{if(skillModal){const role=roles.find(r=>r.key===skillModal.role);await api.updateAgentSkills(role?.level||'E',skillModal.model,skillModal.skills);setSkillModal(null);fetch()}}} style={btn('var(--accent-green)')}>保存</button>
            <button onClick={()=>setSkillModal(null)} style={btn('var(--bg-tertiary)')}>关闭</button>
          </div>
        </div>
      )}

      {/* Prompt Modal */}
      {promptModal && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:16,marginBottom:16}}>
          <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:8}}>
            <span style={{fontWeight:600,fontSize:14}}>系统提示词 — {promptModal.name}</span>
            <button onClick={()=>setPromptModal(null)} style={{...iconBtn,fontSize:16}}>✕</button>
          </div>
          <pre style={{fontSize:10,color:'var(--text-secondary)',whiteSpace:'pre-wrap',maxHeight:300,overflow:'auto',fontFamily:'var(--font-mono)',background:'var(--bg-primary)',padding:10,borderRadius:4}}>
            {promptModal.system_prompt || '(空)'}</pre>
          <div style={{fontSize:10,color:'var(--text-muted)',marginTop:4}}>编辑请直接修改 roles.toml，重启后生效</div>
        </div>
      )}

      {/* Role Table */}
      <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',overflow:'hidden'}}>
        <table style={{width:'100%',borderCollapse:'collapse'}}>
          <thead><tr style={{borderBottom:'1px solid var(--border)',fontSize:11,color:'var(--text-muted)',textAlign:'left'}}>
            <th style={{padding:'8px 10px',width:30}}></th>
            <th style={{padding:'8px 10px',width:120}}>角色</th>
            <th style={{padding:'8px 10px',width:60}}>层级</th>
            <th style={{padding:'8px 10px',width:90}}>人格</th>
            <th style={{padding:'8px 10px',width:120}}>模型</th>
            <th style={{padding:'8px 10px',width:50}}>Skill</th>
            <th style={{padding:'8px 10px',width:120}}>任务活动</th>
            <th style={{padding:'8px 10px'}}>描述</th>
          </tr></thead>
          <tbody>
            {roles.map(r=>{
              const isBusy = r.tasks.running > 0 || r.tasks.pending > 0
              const p = personas[r.persona]
              return (
                <tr key={r.key} style={{borderBottom:'1px solid var(--border)',fontSize:12,background:isBusy?'rgba(88,166,255,0.05)':'transparent'}}>
                  <td style={{padding:'8px 6px',textAlign:'center'}}>
                    <Circle size={10} fill={isBusy?'var(--accent)':'transparent'} color={isBusy?'var(--accent)':'var(--border)'}/>
                  </td>
                  <td style={{padding:'8px 10px'}}>
                    <span style={{fontWeight:600}}>{r.name}</span>
                    <span style={{fontSize:9,color:'var(--text-muted)',display:'block'}}>{r.key}</span>
                  </td>
                  <td style={{padding:'8px 10px'}}>
                    <span style={{background:levelColor(r.level)+'22',color:levelColor(r.level),padding:'1px 6px',borderRadius:3,fontSize:10,fontWeight:600}}>{r.level}</span>
                  </td>
                  <td style={{padding:'8px 10px'}} onClick={e=>e.stopPropagation()}>
                    {editingPersona === r.key ? (
                      <div style={{display:'flex',gap:4,alignItems:'center'}}>
                        <select value={r.persona} onChange={e=>{/* personas are read-only from web */}} style={{...inp,width:80}}>
                          {Object.keys(personas).map(pk=><option key={pk} value={pk}>{personas[pk]?.name||pk}</option>)}
                        </select>
                        <button onClick={()=>setEditingPersona(null)} style={iconBtn}><X size={12}/></button>
                        <span style={{fontSize:9,color:'var(--text-muted)'}}>编辑roles.toml</span>
                      </div>
                    ) : (
                      <span onClick={()=>setEditingPersona(r.key)} style={{cursor:'pointer',display:'flex',alignItems:'center',gap:4}}>
                        <User size={12} color={p?'var(--accent-purple)':'var(--text-muted)'}/>
                        <span style={{fontSize:11,color:p?'var(--text-primary)':'var(--text-muted)'}}>{p?.name || r.persona || '未设置'}</span>
                        <Edit3 size={10} color='var(--text-muted)'/>
                      </span>
                    )}
                  </td>
                  <td style={{padding:'8px 10px'}} onClick={e=>{e.stopPropagation();setEditingRole(editingRole===r.key?null:r.key);setEditModel(r.model)}}>
                    {editingRole === r.key ? (
                      <div style={{display:'flex',gap:4,alignItems:'center'}}>
                        <select value={editModel} onChange={e=>setEditModel(e.target.value)} style={{...inp,width:100}}>
                          {r.available_models.map((m:string)=><option key={m} value={m}>{m}</option>)}
                        </select>
                        <button onClick={()=>handleSwitchModel(r.key,editModel)} style={{...iconBtn,color:'var(--accent-green)'}}><Check size={12}/></button>
                        <button onClick={()=>setEditingRole(null)} style={iconBtn}><X size={12}/></button>
                      </div>
                    ) : (
                      <span style={{fontFamily:'var(--font-mono)',fontSize:11,color:'var(--accent)',cursor:'pointer'}}>{r.model}</span>
                    )}
                  </td>
                  <td style={{padding:'8px 10px'}} onClick={e=>e.stopPropagation()}>
                    <button onClick={()=>handleSkills(r.key,r.model)} title="Skill" style={iconBtn}><Wrench size={14}/></button>
                  </td>
                  <td style={{padding:'8px 10px'}}>
                    <div style={{display:'flex',gap:6,fontSize:11}}>
                      {r.tasks.running>0&&<span style={{color:'var(--accent)'}}>⏳{r.tasks.running}</span>}
                      {r.tasks.pending>0&&<span style={{color:'var(--text-muted)'}}>待{r.tasks.pending}</span>}
                      {r.tasks.done>0&&<span style={{color:'var(--accent-green)'}}>✓{r.tasks.done}</span>}
                      {r.tasks.failed>0&&<span style={{color:'var(--accent-red)'}}>✗{r.tasks.failed}</span>}
                      {r.tasks.total===0&&<span style={{color:'var(--text-muted)'}}>—</span>}
                    </div>
                  </td>
                  <td style={{padding:'8px 10px'}}>
                    <div style={{display:'flex',alignItems:'center',gap:6}}>
                      <span style={{fontSize:11,color:'var(--text-secondary)'}}>{r.description}</span>
                      <button onClick={(e)=>{e.stopPropagation();setPromptModal(r)}} title="提示词" style={iconBtn}><FileText size={14}/></button>
                    </div>
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

const inp = { background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:3,padding:'2px 4px',color:'var(--text-primary)',fontSize:11 } as const
const btn = (bg:string) => ({ background:bg, color:'#fff', border:'none', borderRadius:4, padding:'6px 14px', cursor:'pointer', fontSize:12 })
const iconBtn = { background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2 } as const
