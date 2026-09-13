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
  // ⚠️ 值的类型是 `string[] | null`，**`null` = "读不到，不知道现在绑了什么"**。
  // 原来失败时落 `[]`，于是"读不到"和"一个都没绑"在界面上长得一模一样 ——
  // 用户这时勾任意一个，`updateAgentSkills` 会把后端**原有绑定整体覆盖成签名的这一项**
  // （2026-09-14，外派④扫前端抓出，我核过）。**不知道就不许写**。
  const [matrix, setMatrix] = useState<Record<string,string[] | null>>({})
  const [phaseMatrix, setPhaseMatrix] = useState<Record<string,string[] | null>>({})
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
    const mx: Record<string,string[] | null> = {}
    // 并发拉，别在循环里串行 await（N 个 agent 就是 N 次往返）
    await Promise.all(flat.map(async ag => {
      try { mx[ag.model] = (await api.agentSkills(ag.model)).skills||[] }
      catch { mx[ag.model] = null }        // **不知道**，不是"没有"
    }))
    setMatrix({...mx})
    // 阶段级绑定（跟岗位走）。同样并发拉；读不到就记 null，别落空数组。
    const pm: Record<string,string[] | null> = {}
    await Promise.all(PHASES.map(async ([k]) => {
      try { pm[k] = (await api.phaseSkills(k)).skills||[] }
      catch { pm[k] = null }
    }))
    setPhaseMatrix({...pm})
    // 🔴 **删掉了"加载时自动写入全部技能"那段**（原来在 `flat.length === 1 && 空绑定` 时
    // 直接 PUT 全部技能）。它是**加载路径上的写副作用**：用户把自己清空的绑定
    // 一刷新就全回来了，而且它紧跟在"失败落空数组"之后 ——
    // 单 agent + 一次网络抖动 ⇒ **自动把全部技能写上去**。
    // 那个默认真要给，也该在后端的种子/默认值里给，不该在一次 GET 里偷偷写入。
  }
  useEffect(() => { fetch() }, [])
  const modelLabel = (id: string) => modelDisplay(id) || id

  const toggleSkill = async (model: string, skill: string) => {
    const cur = matrix[model]
    // **不知道就不写** —— 拿 `[]` 当底去 PUT = 把后端原有绑定覆盖成这一项
    if (cur == null) return
    const next = cur.includes(skill) ? cur.filter(s=>s!==skill) : [...cur, skill]
    setMatrix(prev=>({...prev,[model]:next}))
    try { await api.updateAgentSkills(model, next) } catch { setMatrix(prev=>({...prev,[model]:cur})) }
  }
  const assignAll = async (model: string) => {
    if (matrix[model] == null) return       // 同上：不知道当前绑了什么，不许全量覆盖
    const allSkillNames = skills.map(s=>s.name)
    setMatrix(prev=>({...prev,[model]:allSkillNames}))
    try { await api.updateAgentSkills(model, allSkillNames) } catch { fetch() }
  }
  const togglePhaseSkill = async (phase: string, skill: string) => {
    const cur = phaseMatrix[phase]
    if (cur == null) return                 // 同上
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
            const bound = phaseMatrix[key]
            const unknown = bound == null      // 读不到 ⇒ 不许装成"一个都没绑"
            return (
              <div key={key} style={{ marginBottom: 8, padding: '8px 12px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
                <div className="flex-center" style={{ marginBottom: 6 }}>
                  <span className="fw-600 fs-11 flex-1">{label}</span>
                  <span className="fs-10 text-secondary">
                    {unknown ? '读不到绑定' : `${bound!.length}/${skills.length} 技能`}
                  </span>
                </div>
                {unknown && (
                  <div className="fs-10" style={{ marginBottom: 6, color: 'var(--warning, #d48806)' }}>
                    ⚠ 读不到这个阶段的绑定 —— **不让改**（拿空表去写会把真实绑定覆盖掉），刷新重试
                  </div>
                )}
                <div className="flex-center gap-6 flex-wrap" style={{ opacity: unknown ? 0.45 : 1 }}>
                  {skills.map(s => (
                    <Tag.CheckableTag key={s.name} checked={!unknown && bound!.includes(s.name)}
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
            const bound = matrix[a.model]
            const unknown = bound == null     // 同上：读不到 ≠ 没绑
            // 阶段默认优先：配了非空名单的阶段，这份模型绑定在那几个阶段**不生效**。
            // 不标出来就是新的静默失效 —— 界面看着绑了，跑起来用的是阶段那份。
            // ⚠️ `null` = **读不到**，不是"没绑"。`(x?.length ?? 0)` 把"不知道"碾成 0 ⇒
            // 某个阶段的绑定读不出来时，这一行**该出的警示不出** —— 用户看着"没被覆盖"，
            // 跑起来用的是阶段那份（2026-09-14 外派⑦ 抓到）。
            // 同文件模型那条轴（上面的 `unknown`）早就分了三态，这里当时漏了。
            const phaseUnknown = PHASES.some(([k]) => phaseMatrix[k] == null)
            const shadowed = PHASES.filter(([k]) => (phaseMatrix[k]?.length ?? 0) > 0)
            const shadowText = phaseUnknown
              ? '有的阶段默认读不到 —— 这份绑定有没有被覆盖，判不了'
              : shadowed.length === 0 ? ''
              : shadowed.length === PHASES.length ? '已全部被阶段默认覆盖 —— 这份只在阶段留空时才用'
              : `在「${shadowed.map(([,l])=>l).join('、')}」被阶段默认覆盖`
            return (
              <div key={a.model} style={{ marginBottom: 10, padding: '10px 12px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
                <div className="flex-center" style={{ marginBottom: 8 }}>
                  <span className="fw-600 fs-11 flex-1">{modelLabel(a.model)}</span>
                  <span className="fs-10 text-secondary">
                    {unknown ? '读不到绑定' : `${bound!.length}/${skills.length} 技能`}
                  </span>
                  <button onClick={() => assignAll(a.model)} className="btn-sm" style={{ marginLeft: 8 }}
                    disabled={unknown} title={unknown ? '读不到当前绑定，不能全量覆盖' : undefined}>全选</button>
                </div>
                {shadowText && (
                  <div className="fs-10" style={{ marginBottom: 6, color: 'var(--warning, #d48806)' }}>
                    ⚠ {shadowText}
                  </div>
                )}
                {unknown && (
                  <div className="fs-10" style={{ marginBottom: 6, color: 'var(--warning, #d48806)' }}>
                    ⚠ 读不到这个模型的绑定 —— **不让改**（拿空表去写会把真实绑定覆盖掉），刷新重试
                  </div>
                )}
                <div className="flex-center gap-6 flex-wrap" style={{ opacity: unknown ? 0.45 : 1 }}>
                  {skills.map(s => {
                    const has = !unknown && bound!.includes(s.name)
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
