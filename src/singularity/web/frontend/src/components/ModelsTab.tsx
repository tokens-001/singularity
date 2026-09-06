import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { useToast } from './Toast'
import { Plus, Trash2, Search, Download, X } from 'lucide-react'
import { mcn } from '../pages/Config'
import type { ModelInfo, ApiStoreItem } from '../lib/types'

const COST_CN: Record<string,string> = { budget:'省', standard:'标准', premium:'贵' }
const SPEED_CN: Record<string,string> = { fast:'快', medium:'中', slow:'慢' }

const PROVIDERS = [
  { id: 'deepseek', provider: 'DeepSeek', base_url: 'https://api.deepseek.com/v1', api_key_env: 'DEEPSEEK_API_KEY' },
  { id: 'zhipu', provider: '智谱 GLM', base_url: 'https://open.bigmodel.cn/api/paas/v4', api_key_env: 'ZHIPU_API_KEY' },
  { id: 'kimi', provider: 'Moonshot Kimi', base_url: 'https://api.moonshot.cn/v1', api_key_env: 'KIMI_API_KEY' },
  { id: 'openai', provider: 'OpenAI', base_url: 'https://api.openai.com/v1', api_key_env: 'OPENAI_API_KEY' },
  { id: 'anthropic', provider: 'Anthropic', base_url: 'https://api.anthropic.com/v1', api_key_env: 'ANTHROPIC_API_KEY' },
]

export default function ModelsTab() {
  const [models, setModels] = useState<ModelInfo[]>([])
  const [apis, setApis] = useState<ApiStoreItem[]>([])
  const [scanning, setScanning] = useState('')
  const [scanResults, setScanResults] = useState<{models?:ModelInfo[];total?:number;error?:string}|null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [showAddApi, setShowAddApi] = useState(false)
  const [apiForm, setApiForm] = useState({ mode: '', id: '', provider: '', base_url: '', api_key_env: '', api_key: '' })
  const [disabledSet, setDisabledSet] = useState<Set<string>>(new Set())
  const [observerModelId, setObserverModelId] = useState('')
  const addToast = useToast(s => s.add)

  const fetch = async () => {
    const [m, a, ag, obs] = await Promise.all([api.models(), api.apiStore() as Promise<ApiStoreItem[]>, api.agents(), api.observerModel()])
    setModels(m); setApis(a); setObserverModelId(obs as string)
    const ds = new Set<string>()
    for (const d of (ag?._disabled?.any||[])) ds.add(d)
    setDisabledSet(ds)
  }
  useEffect(() => { fetch() }, [])

  const scan = async (apiId: string) => {
    setScanning(apiId)
    try { setScanResults(await api.scanApiStore(apiId) as any) } catch (e) { setScanResults({error:String(e)}) }
    setScanning('')
  }

  const importSelected = async () => {
    const toImport = (scanResults?.models||[]).filter(m=>selected.has(m.id))
    if (!toImport.length) return
    await api.importModels(toImport); setScanResults(null); fetch()
  }

  const pickProvider = (e: any) => {
    const v = e.target.value
    if (v === '__custom__') return setApiForm({ ...apiForm, mode: v, id: '', provider: '', base_url: '', api_key_env: '' })
    const p = PROVIDERS.find(x => x.id === v)
    if (!p) return setApiForm({ ...apiForm, mode: '' })
    setApiForm({ mode: v, id: p.id, provider: p.provider, base_url: p.base_url, api_key_env: p.api_key_env, api_key: apiForm.api_key })
  }

  const addApi = async () => {
    const { id, provider, base_url, api_key_env, api_key } = apiForm
    if (!id) return addToast('请先选择厂家', 'error')
    if (!api_key) return addToast('请输入 API Key', 'error')
    try { await api.addApiStore({ id, provider, base_url, api_key_env, api_key }); addToast('API 已添加', 'success') } catch { addToast('添加失败', 'error') }
    setShowAddApi(false); setApiForm({ mode:'', id:'', provider:'', base_url:'', api_key_env:'', api_key:'' }); fetch()
  }

  return (
    <div>
      <div style={{ marginBottom: 14 }}>
        <div className="flex-center gap-8" style={{ marginBottom: 8 }}>
          <span className="fw-600 fs-12 text-secondary">API 连接</span>
          <button onClick={()=>setShowAddApi(!showAddApi)} className="btn-sm"><Plus size={12}/> 添加</button>
        </div>
        {showAddApi && (
          <div className="flex-center gap-6 flex-wrap" style={{ marginBottom: 8 }}>
            <select value={apiForm.mode} onChange={pickProvider} className="inp-sm">
              <option value="">选择厂家…</option>
              {PROVIDERS.map(p => <option key={p.id} value={p.id}>{p.provider}</option>)}
              <option value="__custom__">自定义…</option>
            </select>
            {apiForm.mode === '__custom__' && (
              <>
                <input placeholder="标识" value={apiForm.id} onChange={e=>setApiForm({...apiForm,id:e.target.value})} className="inp-sm"/>
                <input placeholder="提供商" value={apiForm.provider} onChange={e=>setApiForm({...apiForm,provider:e.target.value})} className="inp-sm"/>
                <input placeholder="基础URL" value={apiForm.base_url} onChange={e=>setApiForm({...apiForm,base_url:e.target.value})} className="inp-sm" style={{width:200}}/>
                <input placeholder="API密钥环境变量" value={apiForm.api_key_env} onChange={e=>setApiForm({...apiForm,api_key_env:e.target.value})} className="inp-sm"/>
              </>
            )}
            <input placeholder="API Key（明文，写入 .env）" type="password" value={apiForm.api_key} onChange={e=>setApiForm({...apiForm,api_key:e.target.value})} className="inp-sm" style={{width:220}}/>
            <button onClick={addApi} className="btn-green">添加</button>
          </div>
        )}
        <div className="flex-center gap-6 flex-wrap">
          {apis.map(a => (
            <div key={a.id} className="flex-center gap-6" style={{ padding: '4px 10px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', fontSize: 11 }}>
              <span className="fw-600">{a.provider||a.id}</span>
              <span className="fs-10" style={{ color: a.status==='active'?'var(--accent-green)':'var(--text-muted)' }}>{a.status==='active'?'●':'○'}</span>
              <button onClick={()=>scan(a.id)} disabled={scanning===a.id} className="btn-sm"><Search size={10}/> {scanning===a.id?'扫描中':'扫描'}</button>
              <button onClick={()=>api.deleteApiStore(a.id).then(fetch)} className="btn-ghost-danger"><Trash2 size={10}/></button>
            </div>
          ))}
        </div>
      </div>

      {scanResults && (
        <div style={{ padding: 10, background: 'var(--bg-secondary)', border: '1px solid var(--accent)', borderRadius: 'var(--radius)', marginBottom: 10 }}>
          <div className="flex-center gap-8" style={{ marginBottom: 6 }}>
            <span className="fw-600 fs-12">{scanResults.error ? `扫描失败: ${scanResults.error}` : `发现 ${scanResults.total} 个模型`}</span>
            {scanResults.models && <>
              <button onClick={()=>setSelected(new Set(scanResults.models!.map(m=>m.id)))} className="btn-sm">全选</button>
              <button onClick={importSelected} disabled={selected.size===0} className="btn-green"><Download size={12}/> 导入 ({selected.size})</button>
            </>}
            <button onClick={()=>setScanResults(null)} className="btn-icon"><X size={14}/></button>
          </div>
          {scanResults.models && (
            <div className="flex-center gap-4 flex-wrap">
              {scanResults.models.map(m => (
                <label key={m.id} className="flex-center gap-4 fs-11" style={{ padding: '3px 8px', background: selected.has(m.id)?'var(--bg-tertiary)':'transparent', borderRadius: 4, cursor: 'pointer' }}>
                  <input type="checkbox" checked={selected.has(m.id)} onChange={()=>{const n=new Set(selected);n.has(m.id)?n.delete(m.id):n.add(m.id);setSelected(n)}}/>
                  {m.display||m.id} <span className="fs-10 text-muted">{m.rating}</span>
                </label>
              ))}
            </div>
          )}
        </div>
      )}

      <div>
        <div className="fw-600 fs-12 text-secondary" style={{ marginBottom: 6, display: 'flex', alignItems: 'center', gap: 8 }}>
          <span>模型目录 ({models.length})</span>
          <span className="flex-1"/>
          <span className="fs-11 fw-400 text-muted">观察者</span>
          <select value={observerModelId} onChange={e => { api.setObserverModel(e.target.value); setObserverModelId(e.target.value) }} className="inp-sm" style={{ width: 'auto' }}>
            <option value="">未设置</option>
            {models.map(m => <option key={m.id} value={m.id}>{mcn(m)}</option>)}
          </select>
        </div>
        {models.map(m => {
          const rf = m.recommended_for||[]
          const disabled = disabledSet.has(m.id)
          const dotColor = disabled ? 'var(--text-muted)' : (m.api_available?'var(--accent-green)':'var(--text-muted)')
          return (
            <div key={m.id} className="card-row" style={{ opacity: disabled?0.5:1 }}>
              <span style={{ color: dotColor, fontSize: 8 }}>{disabled?'○':'●'}</span>
              <span className="fw-500 flex-1">{mcn(m)}</span>
              <span className="fs-10 text-muted">{COST_CN[m.cost||'']||m.cost} · {SPEED_CN[m.speed||'']||m.speed}</span>
              {rf.length > 0 && !(rf.length === 1 && rf[0] === 'any') && (
                <span className="flex-center gap-4">{rf.slice(0,3).map(p=><span key={p} className="card-tag">{p}</span>)}</span>
              )}
              <button onClick={()=>api.deleteModel(m.id).then(fetch)} className="btn-ghost-danger"><Trash2 size={10}/></button>
            </div>
          )
        })}
      </div>
    </div>
  )
}
