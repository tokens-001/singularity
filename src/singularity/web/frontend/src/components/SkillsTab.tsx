import { useState, useEffect } from 'react'
import { Button, Input, Select, Tag } from 'antd'
import { api } from '../lib/api'
import { useRun } from '../lib/toast'
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
  const run = useRun()

  const fetch = async () => {
    const [s, a] = await Promise.all([api.skills() as Promise<SkillInfo[]>, api.agents() as Promise<AgentsData>])
    setSkills(s)
    const flat: AgentItem[] = []; const disabledSet = new Set((a?._disabled?.any||[]) as string[])
    for (const lst of Object.values(a||{})) if (Array.isArray(lst)) {
      (lst as AgentItem[]).forEach(ag => { if (!disabledSet.has(ag.model)) flat.push(ag) })
    }
    setAgents(flat)
    const mx: Record<string,string[]> = {}
    // 并发拉，别在循环里串行 await（N 个 agent 就是 N 次往返）
    await Promise.all(flat.map(async ag => {
      try { mx[ag.model] = (await api.agentSkills(ag.model)).skills||[] } catch { mx[ag.model] = [] }
    }))
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
  const create = async () => {
    if (!(await run(() => api.addSkill(form)))) return
    setShowForm(false); setForm({name:'',description:'',type:'prompt',content:''}); fetch()
  }

  return (
    <div>
      <div className="flex-center gap-8" style={{ marginBottom: 8 }}>
        <span className="fw-600 fs-12 text-secondary">技能 ({skills.length})</span>
        <button onClick={()=>setShowForm(!showForm)} className="btn-sm"><Plus size={12}/> 新建</button>
      </div>
      {showForm && (
        <div className="flex-center gap-6 flex-wrap" style={{ marginBottom: 8, padding: 8, background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
          <Input size="small" style={{ width: 140 }} placeholder="名称" value={form.name} onChange={e=>setForm({...form,name:e.target.value})}/>
          <Input size="small" style={{ width: 200 }} placeholder="描述" value={form.description} onChange={e=>setForm({...form,description:e.target.value})}/>
          <Select size="small" style={{ width: 100 }} value={form.type} onChange={(v)=>setForm({...form,type:v})}
            options={[{ value: 'prompt', label: 'prompt' }, { value: 'tool', label: 'tool' }]}/>
          <Button size="small" type="primary" onClick={create}>创建</Button>
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
                      <Tag.CheckableTag key={s.name} checked={has} onChange={() => toggleSkill(a.model, s.name)}
                        style={{ fontSize: 10, padding: '1px 8px', margin: 0 }}>
                        {SKILL_SHORT[s.name] || s.name}
                      </Tag.CheckableTag>
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
