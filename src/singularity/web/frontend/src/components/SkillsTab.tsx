import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Plus, Check } from 'lucide-react'
import type { ModelInfo, AgentItem, AgentsData, SkillInfo } from '../lib/types'

export default function SkillsTab() {
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [agents, setAgents] = useState<AgentItem[]>([])
  const [models, setModels] = useState<ModelInfo[]>([])
  const [matrix, setMatrix] = useState<Record<string,string[]>>({})
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({ name: '', description: '', type: 'prompt', content: '' })

  const fetch = async () => {
    const [s, a, m] = await Promise.all([api.skills() as Promise<SkillInfo[]>, api.agents() as Promise<AgentsData>, api.models() as Promise<ModelInfo[]>])
    setSkills(s); setModels(Object.values(m||{}))
    const flat: AgentItem[] = []; const disabledSet = new Set((a?._disabled?.any||[]) as string[])
    for (const lst of Object.values(a||{})) if (Array.isArray(lst)) {
      (lst as AgentItem[]).forEach(ag => { if (!disabledSet.has(ag.model)) flat.push(ag) })
    }
    setAgents(flat)
    const mx: Record<string,string[]> = {}
    for (const ag of flat) {
      try { mx[ag.model] = (await api.agentSkills(ag.model)).skills||[] } catch { mx[ag.model] = [] }
    }
    setMatrix({...mx})
    if (flat.length === 1 && s.length > 0 && (mx[flat[0].model]||[]).length === 0) {
      const allNames = s.map(sk=>sk.name)
      try { await api.updateAgentSkills(flat[0].model, allNames); mx[flat[0].model] = allNames; setMatrix({...mx}) } catch {}
    }
  }
  useEffect(() => { fetch() }, [])
  const modelLabel = (id: string) => (models.find(x=>x.id===id)||{}).display || id

  const toggleSkill = async (model: string, skill: string) => {
    const cur = matrix[model]||[]
    const next = cur.includes(skill) ? cur.filter(s=>s!==skill) : [...cur, skill]
    setMatrix(prev=>({...prev,[model]:next}))
    try { await api.updateAgentSkills(model, next) } catch { setMatrix(prev=>({...prev,[model]:cur})) }
  }
  const assignAll = async (model: string) => {
    const allSkillNames = skills.map(s=>s.name)
    setMatrix(prev=>({...prev,[model]:allSkillNames}))
    try { await api.updateAgentSkills(model, allSkillNames) } catch { fetch() }
  }
  const create = async () => { await api.addSkill(form); setShowForm(false); setForm({name:'',description:'',type:'prompt',content:''}); fetch() }

  return (
    <div>
      <div className="flex-center gap-8" style={{ marginBottom: 8 }}>
        <span className="fw-600 fs-12 text-secondary">技能 ({skills.length})</span>
        <button onClick={()=>setShowForm(!showForm)} className="btn-sm"><Plus size={12}/> 新建</button>
      </div>
      {showForm && (
        <div className="flex-center gap-6 flex-wrap" style={{ marginBottom: 8, padding: 8, background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
          <input placeholder="名称" value={form.name} onChange={e=>setForm({...form,name:e.target.value})} className="inp-sm"/>
          <input placeholder="描述" value={form.description} onChange={e=>setForm({...form,description:e.target.value})} className="inp-sm"/>
          <select value={form.type} onChange={e=>setForm({...form,type:e.target.value})} className="inp-sm" style={{width:'auto'}}>
            <option value="prompt">prompt</option><option value="tool">tool</option>
          </select>
          <button onClick={create} className="btn-green">创建</button>
        </div>
      )}
      {skills.length > 0 && (
        <div style={{ overflow: 'auto' }}>
          {agents.length > 0 && (
            <div className="flex-center gap-4 flex-wrap" style={{ marginBottom: 6 }}>
              {agents.slice(0,8).map(a => (
                <button key={a.model} onClick={() => assignAll(a.model)}
                  className="btn-sm" style={{ background: 'transparent', borderColor: 'var(--accent-green)', color: 'var(--accent-green)' }}>
                  全部分配给 {modelLabel(a.model)}
                </button>
              ))}
            </div>
          )}
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)', textAlign: 'left', color: 'var(--text-muted)' }}>
                <th style={{ padding: '4px 8px' }}>技能</th>
                {agents.slice(0,8).map(a => (
                  <th key={a.model} className="fs-9 truncate" style={{ padding: '4px 4px', textAlign: 'center', maxWidth: 80 }}>{modelLabel(a.model)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {skills.map(s => (
                <tr key={s.name} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td className="fw-500" style={{ padding: '4px 8px' }}>{s.name}</td>
                  {agents.slice(0,8).map(a => {
                    const has = (matrix[a.model]||[]).includes(s.name)
                    return (
                      <td key={a.model} style={{ textAlign: 'center', padding: '2px' }}>
                        <button onClick={()=>toggleSkill(a.model, s.name)}
                          style={{ width: 20, height: 20, border: 'none', borderRadius: 3, cursor: 'pointer',
                            background: has?'var(--accent-green)':'var(--bg-tertiary)', color: has?'#fff':'var(--text-muted)', fontSize: 10 }}>
                          {has?<Check size={10}/>:'—'}
                        </button>
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
