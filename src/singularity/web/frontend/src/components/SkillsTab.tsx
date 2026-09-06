import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Plus } from 'lucide-react'
import type { ModelInfo, AgentItem, AgentsData, SkillInfo } from '../lib/types'
import { modelDisplay } from '../pages/Config'

const SKILL_SHORT: Record<string,string> = {
  'code-review': '代码审查', 'creative-brainstorm': '头脑风暴', 'ddd': 'DDD',
  'ponytail': 'Ponytail', 'codegraph': '代码地图',
}

export default function SkillsTab() {
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [agents, setAgents] = useState<AgentItem[]>([])
  const [matrix, setMatrix] = useState<Record<string,string[]>>({})
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({ name: '', description: '', type: 'prompt', content: '' })

  const fetch = async () => {
    const [s, a] = await Promise.all([api.skills() as Promise<SkillInfo[]>, api.agents() as Promise<AgentsData>])
    setSkills(s)
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
  const modelLabel = (id: string) => modelDisplay(id) || id

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
        <div>
          {agents.map(a => {
            const bound = matrix[a.model]||[]
            return (
              <div key={a.model} style={{ marginBottom: 10, padding: '10px 12px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
                <div className="flex-center" style={{ marginBottom: 8 }}>
                  <span className="fw-600 fs-11 flex-1">{modelLabel(a.model)}</span>
                  <span className="fs-10 text-secondary">{bound.length}/{skills.length} 技能</span>
                  <button onClick={() => assignAll(a.model)} className="btn-sm" style={{ marginLeft: 8 }}>全选</button>
                </div>
                <div className="flex-center gap-6 flex-wrap">
                  {skills.map(s => {
                    const has = bound.includes(s.name)
                    return (
                      <button key={s.name} onClick={() => toggleSkill(a.model, s.name)} title={s.description || s.name}
                        style={{ padding: '3px 10px', borderRadius: 999, cursor: 'pointer', fontSize: 10,
                          border: '1px solid ' + (has ? 'var(--accent-green)' : 'var(--border)'),
                          background: has ? 'var(--accent-green)' : 'var(--bg-tertiary)',
                          color: has ? '#fff' : 'var(--text-secondary)' }}>
                        {has ? '✓ ' : ''}{SKILL_SHORT[s.name] || s.name}
                      </button>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
