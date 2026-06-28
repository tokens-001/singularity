import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Cpu, Bot, Wrench, Plus, Trash2, Search, Download, Check, X } from 'lucide-react'

const TABS = [
  { key: 'models', icon: Cpu, label: '模型' },
  { key: 'agents', icon: Bot, label: '智能体' },
  { key: 'skills', icon: Wrench, label: '技能' },
]

export default function Config() {
  const [tab, setTab] = useState('models')
  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <div style={{ display: 'flex', gap: 4, marginBottom: 12 }}>
        {TABS.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
            style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 14px', borderRadius: 'var(--radius)',
              border: 'none', cursor: 'pointer', fontSize: 13, fontWeight: tab===t.key?600:400,
              background: tab===t.key?'var(--accent)':'var(--bg-secondary)',
              color: tab===t.key?'#fff':'var(--text-secondary)' }}>
            <t.icon size={14}/> {t.label}
          </button>
        ))}
      </div>
      {tab === 'models' && <ModelsTab />}
      {tab === 'agents' && <AgentsTab />}
      {tab === 'skills' && <SkillsTab />}
    </div>
  )
}

function ModelsTab() {
  const [models, setModels] = useState<any[]>([])
  const [apis, setApis] = useState<any[]>([])
  const [scanning, setScanning] = useState('')
  const [scanResults, setScanResults] = useState<any>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [showAddApi, setShowAddApi] = useState(false)
  const [apiForm, setApiForm] = useState({ id: '', provider: '', base_url: '', api_key_env: '' })

  const fetch = async () => {
    const [m, a] = await Promise.all([api.models(), api.apiStore()])
    setModels(m); setApis(a)
  }
  useEffect(() => { fetch() }, [])

  const scan = async (apiId: string) => {
    setScanning(apiId)
    try { setScanResults(await api.scanApiStore(apiId)) } catch (e) { setScanResults({error:String(e)}) }
    setScanning('')
  }

  const importSelected = async () => {
    const toImport = (scanResults?.models||[]).filter((m:any)=>selected.has(m.id))
    if (!toImport.length) return
    await api.importModels(toImport); setScanResults(null); fetch()
  }

  const addApi = async () => {
    await api.addApiStore(apiForm); setShowAddApi(false); setApiForm({id:'',provider:'',base_url:'',api_key_env:''}); fetch()
  }

  const sInp = { width: 120, background: 'var(--bg-tertiary)', border: '1px solid var(--border)', borderRadius: 4, padding: '4px 6px', color: 'var(--text-primary)', fontSize: 11 } as const
  const smBtn = { background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', border: '1px solid var(--border)', borderRadius: 4, padding: '3px 8px', cursor: 'pointer', fontSize: 11, display: 'flex', alignItems: 'center', gap: 3 } as const
  const greenBtn = { background: 'var(--accent-green)', color: '#fff', border: 'none', borderRadius: 4, padding: '4px 10px', cursor: 'pointer', fontSize: 11 } as const

  return (
    <div>
      <div style={{ marginBottom: 14 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)' }}>API 连接</span>
          <button onClick={()=>setShowAddApi(!showAddApi)} style={smBtn}><Plus size={12}/> 添加</button>
        </div>
        {showAddApi && (
          <div style={{ display: 'flex', gap: 6, marginBottom: 8, flexWrap: 'wrap' }}>
            <input placeholder="ID" value={apiForm.id} onChange={e=>setApiForm({...apiForm,id:e.target.value})} style={sInp} />
            <input placeholder="Provider" value={apiForm.provider} onChange={e=>setApiForm({...apiForm,provider:e.target.value})} style={sInp} />
            <input placeholder="Base URL" value={apiForm.base_url} onChange={e=>setApiForm({...apiForm,base_url:e.target.value})} style={{...sInp,width:200}} />
            <input placeholder="API Key Env" value={apiForm.api_key_env} onChange={e=>setApiForm({...apiForm,api_key_env:e.target.value})} style={sInp} />
            <button onClick={addApi} style={greenBtn}>添加</button>
          </div>
        )}
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {apis.map((a:any) => (
            <div key={a.id} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 10px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', fontSize: 11 }}>
              <span style={{ fontWeight: 600 }}>{a.provider||a.id}</span>
              <span style={{ color: a.status==='active'?'var(--accent-green)':'var(--text-muted)', fontSize: 10 }}>{a.status==='active'?'●':'○'}</span>
              <button onClick={()=>scan(a.id)} disabled={scanning===a.id} style={smBtn}><Search size={10}/> {scanning===a.id?'扫描中':'扫描'}</button>
              <button onClick={()=>api.deleteApiStore(a.id).then(fetch)} style={{background:'none',border:'none',color:'var(--accent-red)',cursor:'pointer',padding:2}}><Trash2 size={10}/></button>
            </div>
          ))}
        </div>
      </div>

      {scanResults && (
        <div style={{ padding: 10, background: 'var(--bg-secondary)', border: '1px solid var(--accent)', borderRadius: 'var(--radius)', marginBottom: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
            <span style={{ fontSize: 12, fontWeight: 600 }}>{scanResults.error ? `扫描失败: ${scanResults.error}` : `发现 ${scanResults.total} 个模型`}</span>
            {scanResults.models && <>
              <button onClick={()=>setSelected(new Set(scanResults.models.map((m:any)=>m.id)))} style={smBtn}>全选</button>
              <button onClick={importSelected} disabled={selected.size===0} style={greenBtn}><Download size={12}/> 导入 ({selected.size})</button>
            </>}
            <button onClick={()=>setScanResults(null)} style={{background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2}}><X size={14}/></button>
          </div>
          {scanResults.models && (
            <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
              {scanResults.models.map((m:any) => (
                <label key={m.id} style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '3px 8px', background: selected.has(m.id)?'var(--bg-tertiary)':'transparent', borderRadius: 4, cursor: 'pointer', fontSize: 11 }}>
                  <input type="checkbox" checked={selected.has(m.id)} onChange={()=>{const n=new Set(selected);n.has(m.id)?n.delete(m.id):n.add(m.id);setSelected(n)}} />
                  {m.display||m.id} <span style={{ color: 'var(--text-muted)', fontSize: 9 }}>{m.rating}</span>
                </label>
              ))}
            </div>
          )}
        </div>
      )}

      <div>
        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', marginBottom: 6 }}>模型目录 ({models.length})</div>
        {models.map((m:any) => {
          const rf = m.recommended_for||[]
          return (
            <div key={m.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 10px', borderBottom: '1px solid var(--border)', fontSize: 12 }}>
              <span style={{ color: m.api_available?'var(--accent-green)':'var(--text-muted)', fontSize: 8 }}>{m.api_available?'●':'○'}</span>
              <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 500, minWidth: 80 }}>{m.display||m.id}</span>
              <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)', flex: 1 }}>{m.id}</span>
              <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{m.cost} · {m.speed}</span>
              <span style={{ display: 'flex', gap: 2 }}>{rf.slice(0,3).map((p:string)=><span key={p} style={{ padding: '1px 4px', borderRadius: 2, background: 'var(--bg-tertiary)', fontSize: 9, color: 'var(--accent)' }}>{p}</span>)}</span>
              <button onClick={()=>api.deleteModel(m.id).then(fetch)} style={{background:'none',border:'none',color:'var(--accent-red)',cursor:'pointer',padding:2}}><Trash2 size={10}/></button>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function AgentsTab() {
  const [agents, setAgents] = useState<any>({})
  const [models, setModels] = useState<any[]>([])

  const fetch = async () => {
    const [a, m] = await Promise.all([api.agents(), api.models()])
    setAgents(a); setModels(m)
  }
  useEffect(() => { fetch() }, [])

  const allAgents: any[] = []
  for (const lst of Object.values(agents)) if (Array.isArray(lst)) (lst as any[]).forEach((a:any) => allAgents.push(a))
  const activeSet = new Set(allAgents.map((a:any)=>a.model))
  const disabledAny = (agents._disabled?.any||[]) as any[]
  const disabledSet = new Set(Array.isArray(disabledAny)?disabledAny:[])

  const toggle = async (modelId: string, enable: boolean) => {
    if (enable) await api.addAgent({model:modelId})
    else await api.deleteAgent(modelId)
    fetch()
  }

  return (
    <div>
      <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', marginBottom: 6 }}>
        Agent 池 ({allAgents.length} active · {disabledSet.size} 禁用)
      </div>
      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
        {models.map((m:any) => {
          const active = activeSet.has(m.id)
          const disabled = disabledSet.has(m.id)
          return (
            <div key={m.id} onClick={()=>toggle(m.id,!active)}
              style={{ cursor: 'pointer', padding: '6px 10px', border: `1px solid ${active?'var(--accent)':disabled?'rgba(240,97,109,.3)':'var(--border)'}`,
                background: active?'var(--bg-tertiary)':disabled?'rgba(240,97,109,.04)':'transparent', borderRadius: 'var(--radius)', fontSize: 11, minWidth: 160 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ color: m.api_available?'var(--accent-green)':'var(--accent-red)', fontSize: 8 }}>{m.api_available?'●':'○'}</span>
                <span style={{ fontWeight: 500 }}>{m.display||m.id}</span>
                <span style={{ marginLeft: 'auto', fontSize: 9, color: active?'var(--accent)':disabled?'var(--accent-red)':'var(--text-muted)' }}>
                  {active?'ON':disabled?'禁用':'OFF'}
                </span>
              </div>
              <div style={{ fontSize: 9, color: 'var(--text-muted)', marginTop: 2 }}>{m.provider} · {m.cost} · {m.speed}</div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function SkillsTab() {
  const [skills, setSkills] = useState<any[]>([])
  const [agents, setAgents] = useState<any[]>([])
  const [matrix, setMatrix] = useState<Record<string,string[]>>({})
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({ name: '', description: '', type: 'prompt', content: '' })

  const fetch = async () => {
    const [s, a] = await Promise.all([api.skills(), api.agents()])
    setSkills(s)
    const flat: any[] = []
    for (const lst of Object.values(a||{})) if (Array.isArray(lst)) flat.push(...(lst as any[]))
    setAgents(flat)
    const m: Record<string,string[]> = {}
    for (const ag of flat) {
      try { m[ag.model] = (await api.agentSkills(ag.model)).skills||[] } catch { m[ag.model] = [] }
    }
    setMatrix(m)
  }
  useEffect(() => { fetch() }, [])

  const toggleSkill = async (model: string, skill: string) => {
    const cur = matrix[model]||[]
    const next = cur.includes(skill) ? cur.filter((s: string)=>s!==skill) : [...cur, skill]
    setMatrix(prev=>({...prev,[model]:next}))
    try { await api.updateAgentSkills(model, next) } catch { setMatrix(prev=>({...prev,[model]:cur})) }
  }

  const create = async () => { await api.addSkill(form); setShowForm(false); setForm({name:'',description:'',type:'prompt',content:''}); fetch() }
  const sInp = { width: 120, background: 'var(--bg-tertiary)', border: '1px solid var(--border)', borderRadius: 4, padding: '4px 6px', color: 'var(--text-primary)', fontSize: 11 } as const
  const smBtn = { background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', border: '1px solid var(--border)', borderRadius: 4, padding: '3px 8px', cursor: 'pointer', fontSize: 11, display: 'flex', alignItems: 'center', gap: 3 } as const
  const greenBtn = { background: 'var(--accent-green)', color: '#fff', border: 'none', borderRadius: 4, padding: '4px 10px', cursor: 'pointer', fontSize: 11 } as const

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)' }}>技能 ({skills.length})</span>
        <button onClick={()=>setShowForm(!showForm)} style={smBtn}><Plus size={12}/> 新建</button>
      </div>
      {showForm && (
        <div style={{ display: 'flex', gap: 6, marginBottom: 8, flexWrap: 'wrap', padding: 8, background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
          <input placeholder="名称" value={form.name} onChange={e=>setForm({...form,name:e.target.value})} style={sInp} />
          <input placeholder="描述" value={form.description} onChange={e=>setForm({...form,description:e.target.value})} style={sInp} />
          <select value={form.type} onChange={e=>setForm({...form,type:e.target.value})} style={{...sInp,width:'auto'}}>
            <option value="prompt">prompt</option><option value="tool">tool</option>
          </select>
          <button onClick={create} style={greenBtn}>创建</button>
        </div>
      )}
      {skills.length > 0 && (
        <div style={{ overflow: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)', textAlign: 'left', color: 'var(--text-muted)' }}>
                <th style={{ padding: '4px 8px' }}>技能</th>
                {agents.slice(0,8).map((a:any) => (
                  <th key={a.model} style={{ padding: '4px 4px', fontSize: 9, textAlign: 'center', maxWidth: 80, overflow: 'hidden', textOverflow: 'ellipsis' }}>{a.model}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {skills.map((s: any) => (
                <tr key={s.name} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td style={{ padding: '4px 8px', fontWeight: 500 }}>{s.name}</td>
                  {agents.slice(0,8).map((a:any) => {
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
