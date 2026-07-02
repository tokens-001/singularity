import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Plus } from 'lucide-react'
import { ALL_ROLES, ROLE_LABELS, mcn } from '../pages/Config'
import type { ModelInfo, AgentItem, AgentsData } from '../lib/types'

export default function AgentsTab() {
  const [agents, setAgents] = useState<AgentsData>({})
  const [models, setModels] = useState<ModelInfo[]>([])
  const [expanded, setExpanded] = useState<string>('')
  const [showAdd, setShowAdd] = useState(false)

  const fetch = async () => {
    const [a, m] = await Promise.all([api.agents() as Promise<AgentsData>, api.models() as Promise<ModelInfo[]>])
    setAgents(a); setModels(m)
  }
  useEffect(() => { fetch() }, [])

  const allAgents: AgentItem[] = []
  for (const lst of Object.values(agents)) if (Array.isArray(lst)) (lst as AgentItem[]).forEach(a => allAgents.push(a))
  const disabledSet = new Set<string>((agents._disabled?.any||[]) as string[])
  const activeModels = new Set(allAgents.filter(a => !disabledSet.has(a.model)).map(a => a.model))
  const addableModels = models.filter(m => m.api_available && !activeModels.has(m.id) && !disabledSet.has(m.id))

  const toggleRole = async (model: string, role: string, currentRoles: string[]) => {
    const next = currentRoles.includes(role) ? currentRoles.filter(r => r !== role) : [...currentRoles, role]
    await api.updateAgent(model, { roles: next }); fetch()
  }
  const disable = async (model: string) => { await api.deleteAgent(model); fetch() }
  const enable = async (model: string) => { await api.addAgent({model, roles:['daily']}); fetch() }

  return (
    <div>
      <div className="flex-center gap-8" style={{ marginBottom: 8 }}>
        <span className="fw-600 fs-12 text-secondary">已激活 ({allAgents.filter(a=>!disabledSet.has(a.model)).length})</span>
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
          const roles: string[] = a.roles || []
          const isExpanded = expanded === a.model
          return (
            <div key={a.model} className="agent-card">
              <div className="agent-card-header" onClick={() => setExpanded(isExpanded ? '' : a.model)}>
                <span style={{ color: 'var(--accent-green)', fontSize: 8 }}>●</span>
                <span className="fw-600">{mcn(m||{id:a.model})}</span>
                <span className="fs-9 text-muted" style={{ marginLeft: 'auto' }}>{roles.length} 个角色</span>
              </div>
              <div className="fs-9 text-muted" style={{ marginTop: 2 }}>{m?.provider||'?'} · {m?.cost||'?'} · max_turns={a.max_turns||5}</div>
              {isExpanded && (
                <div style={{ marginTop: 6, borderTop: '1px solid var(--border)', paddingTop: 4 }} onClick={e => e.stopPropagation()}>
                  <div className="fs-9 text-muted" style={{ marginBottom: 4 }}>角色分配:</div>
                  <div className="flex-center gap-4 flex-wrap" style={{ marginBottom: 4 }}>
                    {ALL_ROLES.map(r => {
                      const has = roles.includes(r)
                      return (
                        <button key={r} onClick={() => toggleRole(a.model, r, roles)} className="agent-role-btn"
                          style={{ background: has ? 'var(--accent)' : 'var(--bg-secondary)', color: has ? '#fff' : 'var(--text-muted)' }}>
                          {ROLE_LABELS[r]||r}
                        </button>
                      )
                    })}
                  </div>
                  <button onClick={() => disable(a.model)} className="btn-ghost-danger fs-9" style={{ padding: 0 }}>移除</button>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {(agents._disabled?.any||[]).length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div className="fw-600 fs-10 text-muted" style={{ marginBottom: 4 }}>已禁用 ({(agents._disabled?.any||[]).length})</div>
          <div className="flex-center gap-4 flex-wrap">
            {(agents._disabled?.any||[]).map((model: string) => {
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
