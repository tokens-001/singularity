import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Bot, Edit3, Wrench, Check, X, Activity, Circle } from 'lucide-react'

interface RoleInfo {
  key: string; name: string; level: string; description: string
  model: string; available_models: string[]
  tasks: { running: number; pending: number; done: number; failed: number; total: number }
}

export default function AgentDashboard() {
  const [roles, setRoles] = useState<RoleInfo[]>([])
  const [agents, setAgents] = useState<any>({})
  const [loading, setLoading] = useState(true)
  const [editingRole, setEditingRole] = useState<string | null>(null)
  const [editModel, setEditModel] = useState('')
  const [skillModal, setSkillModal] = useState<{role:string;model:string;skills:string[];available:string[]}|null>(null)
  const [expandedRole, setExpandedRole] = useState<string | null>(null)

  const fetch = async () => {
    setLoading(true)
    const [agtData, taskData] = await Promise.all([api.agents(), api.tasks()])
    setAgents(agtData)

    // Count tasks by route_role (or by level if no route_role)
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

    const systemRoles = [
      { key: 'researcher', name: '研究员', level: 'E', desc: '搜集可借鉴方案、理论、替代品' },
      { key: 'system_architect', name: '系统架构师', level: 'D', desc: '模块划分·数据模型·API契约' },
      { key: 'ai_architect', name: 'AI架构师', level: 'D', desc: '模型选型·Prompt体系·Agent拓扑' },
      { key: 'frontend_architect', name: '前端架构师', level: 'D', desc: '组件树·状态管理·路由·性能' },
      { key: 'frontend_engineer', name: '前端工程师', level: 'E', desc: 'UI实现·交互逻辑·可访问性' },
      { key: 'backend_engineer', name: '后端工程师', level: 'E', desc: 'API·数据库·业务逻辑' },
      { key: 'data_engineer', name: '数据工程师', level: 'E+', desc: '向量库·RAG·模型集成' },
      { key: 'devops_engineer', name: 'DevOps工程师', level: 'E', desc: 'CI/CD·容器化·部署' },
      { key: 'qa_engineer', name: 'QA工程师', level: 'D', desc: '验收验证·回归测试' },
      { key: 'security_auditor', name: '安全审计师', level: 'D', desc: '权限·注入·密钥·依赖' },
      { key: 'implementer', name: '执行者', level: 'E', desc: '领单一任务，快进快出' },
      { key: 'debugger', name: '调试者', level: 'E', desc: '定位根因·出补丁·验证' },
      { key: 'builder', name: '构建者', level: 'E+', desc: '复杂模块，多文件改动' },
    ]

    const getModel = (roleKey: string, level: string) => {
      const list = agtData?.[level] || []
      const match = list.find((a:any) => a.default && (a.roles||[]).includes(roleKey))
      if (match) return match.model
      const any = list.find((a:any) => (a.roles||[]).includes(roleKey))
      if (any) return any.model
      return list.find((a:any) => a.default)?.model || list[0]?.model || '无'
    }

    const result: RoleInfo[] = systemRoles.map(sr => ({
      key: sr.key, name: sr.name, level: sr.level, description: sr.desc,
      model: getModel(sr.key, sr.level),
      available_models: (agtData?.[sr.level] || []).map((a:any) => a.model),
      tasks: { ...(taskCounts[sr.key] || { running:0,pending:0,done:0,failed:0 }), total: 0 },
    }))

    // Calculate total for each role
    result.forEach(r => { r.tasks.total = r.tasks.running + r.tasks.pending + r.tasks.done + r.tasks.failed })

    setRoles(result)
    setLoading(false)
  }

  useEffect(() => { fetch() }, [])
  useEffect(() => { const t = setInterval(fetch, 10000); return () => clearInterval(t) }, [])

  const handleSwitchModel = async (roleKey: string, newModel: string) => {
    const role = roles.find(r => r.key === roleKey)
    if (!role) return
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
  const statusColor = (s: string) => s==='running'?'var(--accent)':s==='done'?'var(--accent-green)':s==='failed'?'var(--accent-red)':'var(--text-muted)'

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  // Count total task activity
  const totalActive = roles.reduce((s,r)=>s+r.tasks.running+r.tasks.pending,0)

  return (
    <div>
      <div style={{display:'flex',alignItems:'center',marginBottom:14}}>
        <h2 style={{fontSize:16,fontWeight:600}}>智能体</h2>
        <span style={{marginLeft:8,fontSize:12,color:'var(--text-muted)'}}>
          13 个角色 · {totalActive>0?<span style={{color:'var(--accent)'}}>{totalActive} 个活跃任务</span>:'空闲'}</span>
      </div>

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

      <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',overflow:'hidden'}}>
        <table style={{width:'100%',borderCollapse:'collapse'}}>
          <thead><tr style={{borderBottom:'1px solid var(--border)',fontSize:11,color:'var(--text-muted)',textAlign:'left'}}>
            <th style={{padding:'8px 10px',width:30}}></th>
            <th style={{padding:'8px 10px',width:150}}>角色</th>
            <th style={{padding:'8px 10px',width:60}}>层级</th>
            <th style={{padding:'8px 10px',width:140}}>模型</th>
            <th style={{padding:'8px 10px',width:140}}>任务活动</th>
            <th style={{padding:'8px 10px'}}>描述</th>
            <th style={{padding:'8px 10px',width:50}}></th>
          </tr></thead>
          <tbody>
            {roles.map(r=>{
              const isExpanded = expandedRole === r.key
              const isBusy = r.tasks.running > 0 || r.tasks.pending > 0
              return (
                <tr key={r.key} style={{borderBottom:'1px solid var(--border)',fontSize:12,background:isBusy?'rgba(88,166,255,0.05)':'transparent'}}
                  onClick={()=>setExpandedRole(isExpanded?null:r.key)}>
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
                  <td style={{padding:'8px 10px'}} onClick={e=>{e.stopPropagation();setEditingRole(editingRole===r.key?null:r.key);setEditModel(r.model)}}>
                    {editingRole === r.key ? (
                      <div style={{display:'flex',gap:4,alignItems:'center'}} onClick={e=>e.stopPropagation()}>
                        <select value={editModel} onChange={e=>setEditModel(e.target.value)}
                          style={{background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:3,padding:'2px 4px',color:'var(--text-primary)',fontSize:11,width:110}}>
                          {r.available_models.map((m:string)=><option key={m} value={m}>{m}</option>)}
                        </select>
                        <button onClick={()=>handleSwitchModel(r.key,editModel)} style={{...iconBtn,color:'var(--accent-green)'}}><Check size={12}/></button>
                        <button onClick={()=>setEditingRole(null)} style={iconBtn}><X size={12}/></button>
                      </div>
                    ) : (
                      <span style={{fontFamily:'var(--font-mono)',fontSize:11,color:'var(--accent)',cursor:'pointer'}}>{r.model}</span>
                    )}
                  </td>
                  <td style={{padding:'8px 10px'}}>
                    <div style={{display:'flex',gap:8,fontSize:11}}>
                      {r.tasks.running>0 && <span style={{color:'var(--accent)'}}>⏳{r.tasks.running}</span>}
                      {r.tasks.pending>0 && <span style={{color:'var(--text-muted)'}}>待{r.tasks.pending}</span>}
                      {r.tasks.done>0 && <span style={{color:'var(--accent-green)'}}>✓{r.tasks.done}</span>}
                      {r.tasks.failed>0 && <span style={{color:'var(--accent-red)'}}>✗{r.tasks.failed}</span>}
                      {r.tasks.total===0 && <span style={{color:'var(--text-muted)'}}>—</span>}
                    </div>
                  </td>
                  <td style={{padding:'8px 10px',color:'var(--text-secondary)',fontSize:11}}>{r.description}</td>
                  <td style={{padding:'8px 6px'}} onClick={e=>e.stopPropagation()}>
                    <button onClick={()=>handleSkills(r.key,r.model)} title="Skill" style={iconBtn}><Wrench size={14}/></button>
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

const btn = (bg:string) => ({ background:bg, color:'#fff', border:'none', borderRadius:4, padding:'6px 14px', cursor:'pointer', fontSize:12 })
const iconBtn = { background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2 } as const
