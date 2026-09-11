import { useState, useEffect } from 'react'
import { Select, Tag } from 'antd'
import { api } from '../lib/api'
import { useRun } from '../lib/toast'
import { Plus, Save } from 'lucide-react'
import { mcn } from '../pages/Config'
import type { ModelInfo, AgentItem, AgentsData, PhaseModelsData } from '../lib/types'

// 思考强度档位。各家解释不同（GLM-5.3 只认 low/high/max，DeepSeek 还认 medium，
// Kimi k3 只认 max），代码不替各家做映射 —— 填了不支持的值后端会摘掉重试。
// 实测：GLM-5.3-flash low→max 是 3.5s/0 字思考 vs 9.4s/1912 字；deepseek-v4-flash 切了几乎没差别。
const EFFORT_OPTIONS = [
  { value: '', label: '默认（不指定）' },
  { value: 'low', label: 'low — 少想，快' },
  { value: 'high', label: 'high — 标准' },
  { value: 'max', label: 'max — 多想，慢' },
]

/** 表单草稿 → PUT body。空数组删键（后端 upsert 时删掉该键 = 恢复默认），去重保序。

 * 抽成纯函数是为了能直接单测 —— 组件里那份 Select 的交互在 jsdom 里不好模拟。
 */
export function buildPhasePayload(draft: Record<string, string[]>): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  for (const [key, list] of Object.entries(draft || {})) {
    const kept: string[] = []
    for (const m of list || []) if (m && !kept.includes(m)) kept.push(m)
    if (kept.length) out[key] = kept
  }
  return out
}

export default function AgentsTab() {
  const [agents, setAgents] = useState<AgentsData>({})
  const [models, setModels] = useState<ModelInfo[]>([])
  const [pm, setPm] = useState<PhaseModelsData>({ phases: [], custom: {} })
  const [draft, setDraft] = useState<Record<string, string[]>>({})
  const [saving, setSaving] = useState(false)
  const [expanded, setExpanded] = useState<string>('')
  const [showAdd, setShowAdd] = useState(false)
  const run = useRun()

  const fetch = async () => {
    const [a, m] = await Promise.all([
      api.agents() as Promise<AgentsData>,
      api.models() as Promise<ModelInfo[]>,
    ])
    setAgents(a); setModels(m)
    // 阶段配置**单独取、失败不致命**。塞进上面那个 Promise.all 会出事：
    // 后端没重启（不认识这个新端点）时它会落到 SPA 兜底、返回 HTML，res.json() 抛，
    // 整个 all() reject —— 激活列表和模型列表**一起**变成空的，页面看着像"没有模型"。
    // 一个附加功能取不到，不该把主功能拖空。
    try {
      const p = await api.phaseModels() as PhaseModelsData
      setPm({ phases: p?.phases || [], custom: p?.custom || {} })
      setDraft(p?.custom || {})
    } catch {
      setPm({ phases: [], custom: {} })   // 取不到 = 没有阶段配置 = 用整个池
      setDraft({})
    }
  }
  useEffect(() => { fetch() }, [])

  const allAgents: AgentItem[] = []
  for (const lst of Object.values(agents)) if (Array.isArray(lst)) (lst as AgentItem[]).forEach(a => allAgents.push(a))
  const disabledSet = new Set<string>((agents._disabled?.any||[]) as string[])
  // 只显示模型库里还在的 —— 删模型那条路径会在 _disabled 里留下僵尸标记（后端现在也会清，
  // 但旧数据还在）。不过滤的话界面上会冒出一个模型库根本没有的名字，点它还会弹
  // "空壳 agent"的警告，越看越糊涂。
  const disabledList = ((agents._disabled?.any||[]) as string[]).filter(m => models.some(x => x.id === m))
  const activeModels = new Set(allAgents.filter(a => !disabledSet.has(a.model)).map(a => a.model))
  const addableModels = models.filter(m => m.api_available && !activeModels.has(m.id) && !disabledSet.has(m.id))

  // 只在激活池里的模型能被调度 —— 选项以它为准。但**配置里点名了的**即使已经不在池里
  // 也要列出来标「(已移除)」：悄悄把它换掉等于让"我配过"这件事静默消失（旧的融合页
  // 就是这么干的，配了个已删模型 → 下拉显示空白 → 看不出曾经配过什么）。
  const staleModels = Array.from(new Set(
    Object.values(draft).flat().filter(m => !activeModels.has(m))))
  const phaseOptions = [
    ...Array.from(activeModels).map(m => ({ value: m, label: mcn({ id: m }) })),
    ...staleModels.map(m => ({ value: m, label: `${mcn({ id: m })} (已移除)` })),
  ]

  const savePhaseModels = async () => {
    setSaving(true)
    const ok = await run(() => api.updatePhaseModels(buildPhasePayload(draft)))
    setSaving(false)
    if (ok) fetch()
  }

  const disable = async (model: string) => { if (await run(() => api.deleteAgent(model))) fetch() }
  const enable = async (model: string) => { if (await run(() => api.addAgent({ model }))) fetch() }
  // 空串 = 恢复默认 → 传 null，后端 merge 时删掉该键（merge 语义本来删不掉）
  const setEffort = async (model: string, effort: string) => {
    if (await run(() => api.updateAgent(model, { request_template: { reasoning_effort: effort || null } }))) fetch()
  }

  return (
    <div>
      <div className="flex-center gap-8" style={{ marginBottom: 8 }}>
        <span className="fw-600 fs-12 text-secondary">已激活的模型 ({allAgents.filter(a=>!disabledSet.has(a.model)).length})</span>
        <span className="fs-10 text-muted">这一池管"哪些模型可用"；具体哪个阶段用谁，在下面配</span>
        <button onClick={() => setShowAdd(!showAdd)} className="btn-sm"><Plus size={12}/> 添加</button>
      </div>

      {showAdd && addableModels.length > 0 && (
        <div className="flex-center gap-4 flex-wrap" style={{ marginBottom: 8, padding: 6, background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
          {addableModels.map(m => (
            <button key={m.id} onClick={() => { enable(m.id); setShowAdd(false) }}
              className="btn-sm" style={{ color: 'var(--text-primary)' }}>+ {mcn(m)}</button>
          ))}
        </div>
      )}
      {showAdd && addableModels.length === 0 && (
        <div className="fs-10 text-muted" style={{ marginBottom: 8 }}>没有可添加的模型 — 先去"模型目录"页扫描导入</div>
      )}
      {allAgents.filter(a => !disabledSet.has(a.model)).length === 0 && (
        <div className="fs-11 text-muted" style={{ padding: 20, textAlign: 'center' }}>暂无激活的智能体，点"+ 添加"选择一个模型</div>
      )}

      <div className="flex-center gap-6 flex-wrap">
        {allAgents.filter(a => !disabledSet.has(a.model)).map(a => {
          const m = models.find(x => x.id === a.model)
          const isExpanded = expanded === a.model
          return (
            <div key={a.model} className="agent-card">
              <div className="agent-card-header" onClick={() => setExpanded(isExpanded ? '' : a.model)}>
                <span style={{ color: 'var(--accent-green)', fontSize: 8 }}>●</span>
                <span className="fw-600">{mcn(m||{id:a.model})}</span>
                <span className="fs-10 text-muted" style={{ marginLeft: 'auto' }}>{m?.cost||'?'}</span>
              </div>
              <div className="fs-10 text-muted" style={{ marginTop: 2 }}>{m?.provider||'?'} · max_turns={a.max_turns||5}</div>
              {isExpanded && (
                <div style={{ marginTop: 6, borderTop: '1px solid var(--border)', paddingTop: 4 }} onClick={e => e.stopPropagation()}>
                  <div className="fs-10 text-muted" style={{ marginBottom: 2 }}>思考强度</div>
                  <Select size="small" style={{ width: '100%' }}
                    value={a.request_template?.reasoning_effort || ''}
                    onChange={v => setEffort(a.model, v)}
                    options={EFFORT_OPTIONS}/>
                  <button onClick={() => disable(a.model)} className="btn-ghost-danger fs-10" style={{ padding: 0, marginTop: 4 }}>移除</button>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* 阶段 → 模型。留空 = 用上面那个池，也就是加这个配置之前的行为。 */}
      <div style={{ marginTop: 16, padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 'var(--radius)' }}>
        <div className="flex-center gap-8" style={{ marginBottom: 6 }}>
          <span className="fw-600 fs-12">阶段 → 模型</span>
          <span className="fs-10 text-muted">留空 = 用上面的池（与不配置时完全一致）</span>
          <span style={{ flex: 1 }} />
          <button className="btn-sm" disabled={saving} onClick={savePhaseModels}><Save size={12}/> 保存</button>
        </div>

        {pm.phases.map(p => {
          const picked = draft[p.key] || []
          return (
            <div key={p.key} className="flex-center gap-8" style={{ marginBottom: 6 }}>
              <span className="fs-11 text-muted" style={{ width: 60, flexShrink: 0 }}>{p.label}</span>
              <Select mode="multiple" size="small" style={{ flex: 1 }}
                aria-label={`${p.label}阶段模型`}
                value={picked}
                onChange={v => setDraft({ ...draft, [p.key]: v })}
                placeholder="(不指定 → 用上面的池)"
                options={phaseOptions} />
              <span className="fs-10 text-muted" style={{ width: 96, flexShrink: 0 }}>
                {picked.length ? `首选：${mcn({ id: picked[0] })}` : ''}
              </span>
            </div>
          )
        })}

        <div className="fs-10 text-muted" style={{ marginTop: 4 }}>
          {pm.phases.map(p => `${p.label}：${p.hint}`).join('；')}
        </div>
      </div>

      {disabledList.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div className="fw-600 fs-10 text-muted" style={{ marginBottom: 4 }}>已禁用 ({disabledList.length})</div>
          <div className="flex-center gap-4 flex-wrap">
            {disabledList.map((model: string) => {
              const m = models.find(x => x.id === model)
              return (
                <button key={model} onClick={() => enable(model)}
                  className="btn-sm" style={{ background: 'transparent', color: 'var(--text-muted)' }}>
                  {mcn(m||{id:model})} ↗
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
