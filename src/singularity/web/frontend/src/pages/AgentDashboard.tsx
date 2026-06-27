import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Bot, Edit3, Wrench, Check, X, Cpu, FileText } from 'lucide-react'

// Role definitions from roles.toml — loaded via API
interface RoleInfo {
  key: string; name: string; level: string; description: string
  model: string; available_models: string[]
  skills: string[]; system_prompt: string
}

export default function AgentDashboard() {
  const [roles, setRoles] = useState<RoleInfo[]>([])
  const [agents, setAgents] = useState<any>({})
  const [loading, setLoading] = useState(true)
  const [editingRole, setEditingRole] = useState<string | null>(null)
  const [editModel, setEditModel] = useState('')
  const [skillModal, setSkillModal] = useState<{role:string;model:string;skills:string[];available:string[]}|null>(null)
  const [viewPrompt, setViewPrompt] = useState<string | null>(null)

  const fetch = async () => {
    setLoading(true)
    const [agtData, skillData] = await Promise.all([api.agents(), api.skills()])
    setAgents(agtData)

    // Build role list from registered agents and their roles
    const roleMap: Record<string, any> = {}
    for (const lvl of ['D','E+','E']) {
      for (const a of (agtData?.[lvl] || [])) {
        const modelRoles = a.roles || []
        for (const r of modelRoles) {
          if (!roleMap[r]) roleMap[r] = { models: [], default_model: a.default ? a.model : '' }
          if (!roleMap[r].models.includes(a.model)) roleMap[r].models.push(a.model)
          if (a.default) roleMap[r].default_model = a.model
        }
        // Also register the model itself as available
        if (!roleMap['_models']) roleMap['_models'] = []
        if (!roleMap['_models'].includes(a.model)) roleMap['_models'].push(a.model)
      }
    }

    // Also add the architect roles from our system
    const systemRoles = [
      { key: 'researcher', name: '研究员', level: 'E', desc: '搜集可借鉴方案、理论、替代品' },
      { key: 'system_architect', name: '系统架构师', level: 'D', desc: '模块划分·数据模型·API契约·技术栈' },
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

    const result: RoleInfo[] = systemRoles.map(sr => ({
      key: sr.key, name: sr.name, level: sr.level, description: sr.desc,
      model: getActiveModelForRole(sr.key, sr.level, agtData),
      available_models: getAvailableModels(sr.level, agtData),
      skills: [],
      system_prompt: '',
    }))

    setRoles(result)
    setLoading(false)
  }

  useEffect(() => { fetch() }, [])

  const getActiveModelForRole = (roleKey: string, level: string, agtData: any) => {
    const agents_ = agtData?.[level] || []
    // Prefer default agent with matching role
    const match = agents_.find((a:any) => a.default && (a.roles||[]).includes(roleKey))
    if (match) return match.model
    // Fallback to any agent with matching role
    const any = agents_.find((a:any) => (a.roles||[]).includes(roleKey))
    if (any) return any.model
    // Fallback to default agent at this level
    const def = agents_.find((a:any) => a.default)
    return def?.model || (agents_[0]?.model || '无')
  }

  const getAvailableModels = (level: string, agtData: any) => {
    return (agtData?.[level] || []).map((a:any) => a.model)
  }

  const handleSwitchModel = async (roleKey: string, newModel: string) => {
    // Update the agent's roles to include/exclude this role
    const role = roles.find(r => r.key === roleKey)
    if (!role) return
    // Add role tag to the new model's agent
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

  const handleSaveSkills = async () => {
    if (!skillModal) return
    const role = roles.find(r => r.key === skillModal.role)
    await api.updateAgentSkills(role?.level||'E', skillModal.model, skillModal.skills)
    setSkillModal(null); fetch()
  }

  const levelColor = (l: string) => l==='D'?'#f0883e':l==='E+'?'#a371f7':'#58a6ff'

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:4}}>智能体管理</h2>
      <div style={{fontSize:12,color:'var(--text-muted)',marginBottom:14}}>
        管理 13 个角色的模型绑定、Skill 分配和系统提示词。模型注册在 <a href="/models" style={{color:'var(--accent)'}}>模型管理</a>，API 连接在 <a href="/settings" style={{color:'var(--accent)'}}>配置</a>。
      </div>

      {/* Skill Modal */}
      {skillModal && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--accent-purple)',borderRadius:'var(--radius)',padding:16,marginBottom:16}}>
          <div style={{fontSize:14,fontWeight:600,marginBottom:8}}>
            Skills — {roles.find(r=>r.key===skillModal!.role)?.name} · {skillModal.model}</div>
          <div style={{display:'flex',flexWrap:'wrap',gap:6,marginBottom:10}}>
            {skillModal.skills.length===0 && <span style={{fontSize:11,color:'var(--text-muted)'}}>未分配</span>}
            {skillModal.skills.map((s:string)=><span key={s} onClick={()=>setSkillModal({...skillModal,skills:skillModal.skills.filter((x:string)=>x!==s)})} style={{background:'var(--accent-purple)',color:'#fff',padding:'4px 8px',borderRadius:4,fontSize:11,cursor:'pointer'}}>{s} ✕</span>)}
          </div>
          <div style={{fontSize:11,color:'var(--text-muted)',marginBottom:4}}>可用:</div>
          <div style={{display:'flex',flexWrap:'wrap',gap:6}}>
            {(skillModal.available||[]).filter((s:string)=>!skillModal.skills.includes(s)).map((s:string)=><span key={s} onClick={()=>setSkillModal({...skillModal,skills:[...skillModal.skills,s]})} style={{background:'var(--bg-tertiary)',padding:'4px 8px',borderRadius:4,fontSize:11,cursor:'pointer',color:'var(--text-secondary)'}}>+ {s}</span>)}
          </div>
          <div style={{display:'flex',gap:8,marginTop:10}}>
            <button onClick={handleSaveSkills} style={btn('var(--accent-green)')}>保存</button>
            <button onClick={()=>setSkillModal(null)} style={btn('var(--bg-tertiary)')}>关闭</button>
          </div>
        </div>
      )}

      {/* Role table */}
      <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',overflow:'hidden'}}>
        <table style={{width:'100%',borderCollapse:'collapse'}}>
          <thead><tr style={{borderBottom:'1px solid var(--border)',fontSize:11,color:'var(--text-muted)',textAlign:'left'}}>
            <th style={{padding:'8px 10px',width:150}}>角色</th>
            <th style={{padding:'8px 10px',width:60}}>层级</th>
            <th style={{padding:'8px 10px',width:200}}>使用模型</th>
            <th style={{padding:'8px 10px'}}>描述</th>
            <th style={{padding:'8px 10px',width:80}}>操作</th>
          </tr></thead>
          <tbody>
            {roles.map(r=>(
              <tr key={r.key} style={{borderBottom:'1px solid var(--border)',fontSize:12}}>
                <td style={{padding:'8px 10px',display:'flex',alignItems:'center',gap:6}}>
                  <Bot size={14} color={levelColor(r.level)}/>
                  <span style={{fontWeight:600}}>{r.name}</span>
                  <span style={{fontSize:9,color:'var(--text-muted)',fontFamily:'var(--font-mono)'}}>{r.key}</span>
                </td>
                <td style={{padding:'8px 10px'}}>
                  <span style={{background:levelColor(r.level)+'22',color:levelColor(r.level),padding:'1px 6px',borderRadius:3,fontSize:10,fontWeight:600}}>{r.level}</span>
                </td>
                <td style={{padding:'8px 10px'}}>
                  {editingRole === r.key ? (
                    <div style={{display:'flex',gap:4,alignItems:'center'}}>
                      <select value={editModel} onChange={e=>setEditModel(e.target.value)}
                        style={{background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:3,padding:'3px 6px',color:'var(--text-primary)',fontSize:11}}>
                        {r.available_models.map((m:string)=><option key={m} value={m}>{m}</option>)}
                      </select>
                      <button onClick={()=>handleSwitchModel(r.key,editModel)} style={{...iconBtn,color:'var(--accent-green)'}}><Check size={14}/></button>
                      <button onClick={()=>setEditingRole(null)} style={iconBtn}><X size={14}/></button>
                    </div>
                  ) : (
                    <div style={{display:'flex',alignItems:'center',gap:4}}>
                      <span style={{fontFamily:'var(--font-mono)',fontSize:11,color:'var(--accent)',cursor:'pointer'}}
                        onClick={()=>{setEditingRole(r.key);setEditModel(r.model)}}>{r.model}</span>
                      <button onClick={()=>{setEditingRole(r.key);setEditModel(r.model)}} style={iconBtn}><Edit3 size={10}/></button>
                    </div>
                  )}
                </td>
                <td style={{padding:'8px 10px',color:'var(--text-secondary)',fontSize:11}}>{r.description}</td>
                <td style={{padding:'8px 10px'}}>
                  <div style={{display:'flex',gap:4}}>
                    <button onClick={()=>handleSkills(r.key,r.model)} title="Skills" style={iconBtn}><Wrench size={14}/></button>
                    <button onClick={()=>setViewPrompt(viewPrompt===r.key?null:r.key)} title="提示词" style={{...iconBtn,color:viewPrompt===r.key?'var(--accent)':'var(--text-secondary)'}}><FileText size={14}/></button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Prompt preview */}
      {viewPrompt && (
        <div style={{marginTop:12,background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
          <div style={{fontSize:13,fontWeight:600,marginBottom:6}}>
            系统提示词 — {roles.find(r=>r.key===viewPrompt)?.name}</div>
          <pre style={{fontSize:10,color:'var(--text-secondary)',whiteSpace:'pre-wrap',maxHeight:300,overflow:'auto',fontFamily:'var(--font-mono)'}}>
            从 roles.toml 加载...（Web 界面暂不支持编辑提示词，请直接编辑 roles.toml）
          </pre>
        </div>
      )}
    </div>
  )
}

const btn = (bg:string) => ({ background:bg, color:'#fff', border:'none', borderRadius:4, padding:'6px 14px', cursor:'pointer', fontSize:12 })
const iconBtn = { background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2 } as const
