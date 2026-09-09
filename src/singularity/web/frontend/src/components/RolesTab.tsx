import { useState, useEffect } from 'react'
import { Button, Input, Modal, Popconfirm, Select } from 'antd'
import { api } from '../lib/api'
import { useRun } from '../lib/toast'
import { Plus, Trash2 } from 'lucide-react'

type RoleInfo = { key: string; name: string; description: string; system_prompt: string; capabilities?: string[] }
type PhaseInfo = { key: string; label: string }

export default function RolesTab() {
  const [roles, setRoles] = useState<Record<string, RoleInfo>>({})
  const [editing, setEditing] = useState<string>('')
  const [draft, setDraft] = useState({ name: '', description: '', system_prompt: '' })
  const [adding, setAdding] = useState(false)
  const [newKey, setNewKey] = useState('')
  const [phases, setPhases] = useState<PhaseInfo[]>([])
  const [phaseMap, setPhaseMap] = useState<Record<string, string>>({})
  const run = useRun()

  const fetch = async () => {
    const [d, p] = await Promise.all([api.roles(), api.phaseRoles()])
    setRoles(d?.roles || {})
    setPhases(p?.phases || [])
    setPhaseMap({ ...(p?.defaults || {}), ...(p?.custom || {}) })
  }
  useEffect(() => { fetch() }, [])

  const setPhaseRole = async (key: string, role: string) => {
    const next = { ...phaseMap, [key]: role }
    setPhaseMap(next)
    await run(() => api.updatePhaseRoles(next))
  }

  const open = (key: string) => {
    const r = roles[key]
    setDraft({ name: r.name || '', description: r.description || '', system_prompt: r.system_prompt || '' })
    setEditing(key)
  }

  const save = async () => {
    if (await run(() => api.updateRole(editing, draft))) { setEditing(''); fetch() }
  }

  const create = async () => {
    const key = newKey.trim()
    if (!key) return
    if (await run(() => api.createRole({ key, name: draft.name || key, description: draft.description, system_prompt: draft.system_prompt }))) {
      setAdding(false); setNewKey(''); setDraft({ name: '', description: '', system_prompt: '' }); fetch()
    }
  }

  const remove = async (key: string) => {
    if (await run(() => api.deleteRole(key))) fetch()
  }

  return (
    <div style={{ padding: 4 }}>
      <div className="flex-center gap-8" style={{ marginBottom: 10 }}>
        <span className="fw-600 fs-12 text-secondary">角色 ({Object.keys(roles).length})</span>
        <Button size="small" icon={<Plus size={12}/>} onClick={() => { setDraft({ name: '', description: '', system_prompt: '' }); setNewKey(''); setAdding(true) }}>
          新增角色
        </Button>
      </div>

      <div className="fs-10 text-muted" style={{ marginBottom: 10 }}>
        角色 = 一段约束提示词。**绑定研发阶段**（不是绑定模型 —— 模型回答"谁来做"，角色回答"做什么、不做什么"）。
      </div>

      <div style={{ border: '1px solid var(--border)', borderRadius: 8, padding: '8px 10px', marginBottom: 14, background: '#faf9f5' }}>
        <div className="fw-600 fs-12" style={{ marginBottom: 6 }}>阶段 → 角色</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
          {phases.map(p => (
            <div key={p.key} className="flex-center gap-6">
              <span className="fs-11 text-muted" style={{ width: 32 }}>{p.label}</span>
              <Select size="small" style={{ width: 150 }} value={phaseMap[p.key] || ''}
                onChange={v => setPhaseRole(p.key, v)}
                options={[{ value: '', label: '(不注入角色)' },
                  ...Object.entries(roles).map(([k, r]) => ({ value: k, label: r.name || k }))]} />
            </div>
          ))}
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {Object.entries(roles).map(([key, r]) => (
          <div key={key} style={{ border: '1px solid var(--border)', borderRadius: 8, padding: '8px 10px', background: '#fff' }}>
            <div className="flex-center gap-8">
              <span className="fw-600 fs-12">{r.name || key}</span>
              <span className="fs-10 text-muted" style={{ fontFamily: 'var(--font-mono)' }}>{key}</span>
              <span className="fs-10 text-muted" style={{ flex: 1 }}>{r.description}</span>
              <Button size="small" type="link" onClick={() => open(key)}>编辑</Button>
              <Popconfirm title={`删除角色 ${key}？`} description="roles.toml 不动，只写覆盖层。" onConfirm={() => remove(key)} okText="删除" cancelText="取消">
                <Button size="small" type="link" danger icon={<Trash2 size={11}/>} />
              </Popconfirm>
            </div>
            <div className="fs-10 text-muted" style={{ marginTop: 4, fontFamily: 'var(--font-mono)', whiteSpace: 'pre-wrap', maxHeight: 40, overflow: 'hidden' }}>
              {(r.system_prompt || '(无提示词)').slice(0, 120)}
            </div>
          </div>
        ))}
      </div>

      <Modal open={!!editing} title={`编辑角色 · ${editing}`} onCancel={() => setEditing('')} onOk={save} okText="保存" width={720}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <Input addonBefore="名称" value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} />
          <Input addonBefore="说明" value={draft.description} onChange={e => setDraft({ ...draft, description: e.target.value })} />
          <Input.TextArea rows={14} value={draft.system_prompt} onChange={e => setDraft({ ...draft, system_prompt: e.target.value })}
            placeholder="角色提示词（这段会被拼进该阶段任务的 prompt 前面）" style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }} />
        </div>
      </Modal>

      <Modal open={adding} title="新增角色" onCancel={() => setAdding(false)} onOk={create} okText="创建" width={720}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <Input addonBefore="key" value={newKey} onChange={e => setNewKey(e.target.value)} placeholder="英文标识，如 architect" />
          <Input addonBefore="名称" value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} />
          <Input addonBefore="说明" value={draft.description} onChange={e => setDraft({ ...draft, description: e.target.value })} />
          <Input.TextArea rows={12} value={draft.system_prompt} onChange={e => setDraft({ ...draft, system_prompt: e.target.value })}
            placeholder="角色提示词" style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }} />
        </div>
      </Modal>
    </div>
  )
}
