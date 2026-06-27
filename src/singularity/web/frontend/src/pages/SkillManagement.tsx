import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Wrench, Plus, Trash2, Check, X } from 'lucide-react'

export default function SkillManagement() {
  const [skills, setSkills] = useState<any[]>([])
  const [agents, setAgents] = useState<any[]>([])
  const [matrix, setMatrix] = useState<Record<string, string[]>>({})
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({ name: '', description: '', type: 'prompt', content: '' })
  const [selectedSkill, setSelectedSkill] = useState<any>(null)
  const [loadingMatrix, setLoadingMatrix] = useState<Record<string, boolean>>({})

  const fetch = async () => {
    const [s, a] = await Promise.all([api.skills(), api.agents()])
    setSkills(s)
    const flat: any[] = []
    for (const lvl of ['D','E+','E']) for (const agent of (a?.[lvl]||[])) flat.push({...agent, level: lvl})
    setAgents(flat)

    // Load skills per agent
    const m: Record<string, string[]> = {}
    for (const ag of flat) {
      try {
        const d = await api.agentSkills(ag.level, ag.model)
        m[ag.model] = d.skills || []
      } catch { m[ag.model] = [] }
    }
    setMatrix(m)
  }

  useEffect(() => { fetch() }, [])

  const handleAdd = async () => {
    await api.addSkill(form)
    setShowForm(false); setForm({ name: '', description: '', type: 'prompt', content: '' }); fetch()
  }

  const toggleSkill = async (model: string, level: string, skillName: string) => {
    const key = `${level}/${model}`
    setLoadingMatrix(prev => ({...prev, [key]: true}))
    const cur = matrix[model] || []
    const next = cur.includes(skillName) ? cur.filter(s => s !== skillName) : [...cur, skillName]
    setMatrix(prev => ({...prev, [model]: next}))
    try { await api.updateAgentSkills(level, model, next) } catch { setMatrix(prev => ({...prev, [model]: cur})) }
    setLoadingMatrix(prev => ({...prev, [key]: false}))
  }

  return (
    <div>
      <div style={{display:'flex',alignItems:'center',marginBottom:14}}>
        <h2 style={{fontSize:16,fontWeight:600}}>技能管理 ({skills.length})</h2>
        <button onClick={()=>setShowForm(!showForm)}
          style={{marginLeft:'auto',background:'var(--accent)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'6px 12px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:4}}>
          <Plus size={14}/> 新建 Skill</button>
      </div>

      {showForm && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14,marginBottom:14,display:'flex',flexDirection:'column',gap:8}}>
          <input placeholder="Skill 名称" value={form.name} onChange={e=>setForm({...form,name:e.target.value})} style={inp}/>
          <input placeholder="描述" value={form.description} onChange={e=>setForm({...form,description:e.target.value})} style={inp}/>
          <select value={form.type} onChange={e=>setForm({...form,type:e.target.value})} style={inp}>
            <option value="prompt">prompt — 注入系统提示词</option>
            <option value="tool">tool — 注册为函数调用工具</option>
          </select>
          <textarea placeholder="内容 (Markdown)" value={form.content} onChange={e=>setForm({...form,content:e.target.value})} style={{...inp,minHeight:80}}/>
          <div style={{display:'flex',gap:8}}>
            <button onClick={handleAdd} style={btn('var(--accent-green)')}>保存</button>
            <button onClick={()=>setShowForm(false)} style={btn('var(--bg-tertiary)')}>取消</button>
          </div>
        </div>
      )}

      {/* Skills list */}
      <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(300px,1fr))',gap:10,marginBottom:20}}>
        {skills.map(s=>(
          <div key={s.name} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14,cursor:'pointer'}}
            onClick={()=>setSelectedSkill(selectedSkill?.name===s.name?null:s)}>
            <div style={{display:'flex',justifyContent:'space-between',alignItems:'start'}}>
              <div>
                <div style={{fontWeight:600,fontSize:13,display:'flex',alignItems:'center',gap:6}}>
                  <Wrench size={14} color='var(--accent-purple)'/>{s.name}</div>
                <div style={{fontSize:11,color:'var(--text-secondary)',marginTop:2}}>{s.description||s.type}</div>
              </div>
              <button onClick={e=>{e.stopPropagation();api.deleteSkill(s.name).then(fetch)}}
                style={{background:'none',border:'none',color:'var(--text-muted)',cursor:'pointer',padding:2}}><Trash2 size={14}/></button>
            </div>
            {selectedSkill?.name===s.name && (
              <pre style={{fontSize:10,color:'var(--text-secondary)',marginTop:8,whiteSpace:'pre-wrap',maxHeight:200,overflow:'auto',background:'var(--bg-primary)',padding:8,borderRadius:4}}>
                {s.content||JSON.stringify(s,null,2)}</pre>
            )}
          </div>
        ))}
      </div>

      {/* Agent × Skill matrix */}
      <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',overflow:'hidden'}}>
        <div style={{fontSize:14,fontWeight:600,padding:'10px 14px',borderBottom:'1px solid var(--border)'}}>Agent × Skill 绑定</div>
        <div style={{overflowX:'auto'}}>
          <table style={{width:'100%',borderCollapse:'collapse',fontSize:11}}>
            <thead>
              <tr style={{borderBottom:'1px solid var(--border)'}}>
                <th style={{padding:'6px 10px',textAlign:'left',color:'var(--text-muted)',minWidth:120}}>Agent</th>
                <th style={{padding:'6px 10px',textAlign:'left',color:'var(--text-muted)',minWidth:50}}>层</th>
                {skills.map(s=>(
                  <th key={s.name} style={{padding:'4px 6px',textAlign:'center',color:'var(--text-muted)',writingMode:'vertical-rl',fontSize:10,fontWeight:400}}>{s.name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {agents.map(a=>{
                const curSkills = matrix[a.model] || []
                return (
                  <tr key={`${a.level}/${a.model}`} style={{borderBottom:'1px solid var(--border)'}}>
                    <td style={{padding:'6px 10px',fontFamily:'var(--font-mono)',fontSize:11}}>{a.model}</td>
                    <td style={{padding:'6px 10px'}}>
                      <span style={{background:a.level==='D'?'#f0883e22':a.level==='E+'?'#a371f722':'#58a6ff22',color:a.level==='D'?'#f0883e':a.level==='E+'?'#a371f7':'#58a6ff',padding:'1px 5px',borderRadius:3,fontSize:9,fontWeight:600}}>{a.level}</span>
                    </td>
                    {skills.map(s=>{
                      const has = curSkills.includes(s.name)
                      return (
                        <td key={s.name} style={{padding:'4px 6px',textAlign:'center'}}>
                          <button onClick={()=>toggleSkill(a.model, a.level, s.name)}
                            style={{background:has?'var(--accent-green)':'var(--bg-tertiary)',border:'none',borderRadius:3,width:22,height:22,cursor:'pointer',display:'inline-flex',alignItems:'center',justifyContent:'center'}}>
                            {has ? <Check size={12} color='#fff'/> : <span style={{color:'var(--text-muted)',fontSize:10}}>—</span>}
                          </button>
                        </td>
                      )
                    })}
                  </tr>
                )
              })}
              {agents.length===0 && (
                <tr><td colSpan={3+skills.length} style={{padding:20,textAlign:'center',color:'var(--text-muted)'}}>暂无 Agent，请先在模型管理页面配置</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

const inp = { background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:4,padding:'6px 8px',color:'var(--text-primary)',fontSize:12 } as const
const btn = (bg:string) => ({ background:bg, color:'#fff', border:'none', borderRadius:4, padding:'6px 14px', cursor:'pointer', fontSize:12 })
