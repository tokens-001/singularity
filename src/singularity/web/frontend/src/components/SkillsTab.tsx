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

/** 与后端 phase_models.PHASES 对齐。 */
const PHASES: [string,string][] = [
  ['researching','调研'], ['planning','架构'], ['executing','实现'],
  ['reviewing','审查'], ['extract','融合提取'],
]

export default function SkillsTab() {
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [agents, setAgents] = useState<AgentItem[]>([])
  const [matrix, setMatrix] = useState<Record<string,string[]>>({})
  const [phaseMatrix, setPhaseMatrix] = useState<Record<string,string[]>>({})
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
    // 阶段级绑定（跟岗位走）。同样并发拉，失败时留空而不是让整页挂掉。
    const pm: Record<string,string[]> = {}
    await Promise.all(PHASES.map(async ([k]) => {
      try { pm[k] = (await api.phaseSkills(k)).skills||[] } catch { pm[k] = [] }
    }))
    setPhaseMatrix({...pm})
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
  const togglePhaseSkill = async (phase: string, skill: string) => {
    const cur = phaseMatrix[phase]||[]
    const next = cur.includes(skill) ? cur.filter(s=>s!==skill) : [...cur, skill]
    setPhaseMatrix(prev=>({...prev,[phase]:next}))
    try { await api.updatePhaseSkills(phase, next) } catch { setPhaseMatrix(prev=>({...prev,[phase]:cur})) }
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
      <div className="fs-10 text-muted" style={{ marginBottom: 8 }}>
        技能 = <b>会什么</b>（能力）；角色 = 这个阶段<b>该干什么</b>（职责，见「角色」页）。
        提示词类约束请做成角色，技能只用来加真工具。<br/>
        <b>阶段默认优先</b>：某阶段配了就用它，没配才回落到下面按模型的那份。
        清空某个阶段即可让它恢复走模型绑定。
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
          <div className="fs-11 fw-600 text-secondary" style={{ margin: '12px 0 6px' }}>
            阶段默认（跟岗位走）
          </div>
          {PHASES.map(([key, label]) => {
            const bound = phaseMatrix[key]||[]
            return (
              <div key={key} style={{ marginBottom: 8, padding: '8px 12px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
                <div className="flex-center" style={{ marginBottom: 6 }}>
                  <span className="fw-600 fs-11 flex-1">{label}</span>
                  <span className="fs-10 text-secondary">{bound.length}/{skills.length} 技能</span>
                </div>
                <div className="flex-center gap-6 flex-wrap">
                  {skills.map(s => (
                    <Tag.CheckableTag key={s.name} checked={bound.includes(s.name)}
                      onChange={() => togglePhaseSkill(key, s.name)}
                      style={{ fontSize: 10, padding: '1px 8px', margin: 0 }}>
                      {SKILL_SHORT[s.name] || s.name}
                    </Tag.CheckableTag>
                  ))}
                </div>
              </div>
            )
          })}
          <div className="fs-11 fw-600 text-secondary" style={{ margin: '12px 0 6px' }}>
            按模型（例外，覆盖阶段默认）
          </div>
          {agents.map(a => {
            const bound = matrix[a.model]||[]
            // 阶段默认优先：配了非空名单的阶段，这份模型绑定在那几个阶段**不生效**。
            // 不标出来就是新的静默失效 —— 界面看着绑了，跑起来用的是阶段那份。
            const shadowed = PHASES.filter(([k]) => (phaseMatrix[k]||[]).length > 0)
            const shadowText = shadowed.length === 0 ? ''
              : shadowed.length === PHASES.length ? '已全部被阶段默认覆盖 —— 这份只在阶段留空时才用'
              : `在「${shadowed.map(([,l])=>l).join('、')}」被阶段默认覆盖`
            return (
              <div key={a.model} style={{ marginBottom: 10, padding: '10px 12px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
                <div className="flex-center" style={{ marginBottom: 8 }}>
                  <span className="fw-600 fs-11 flex-1">{modelLabel(a.model)}</span>
                  <span className="fs-10 text-secondary">{bound.length}/{skills.length} 技能</span>
                  <button onClick={() => assignAll(a.model)} className="btn-sm" style={{ marginLeft: 8 }}>全选</button>
                </div>
                {shadowText && (
                  <div className="fs-10" style={{ marginBottom: 6, color: 'var(--warning, #d48806)' }}>
                    ⚠ {shadowText}
                  </div>
                )}
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
