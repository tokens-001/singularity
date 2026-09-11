import { useState, useEffect } from 'react'
import { Button, Input, Select } from 'antd'
import { api } from '../lib/api'
import { useToast, useModal, useRun } from '../lib/toast'
import { Plus, Trash2, Search, Download, X } from 'lucide-react'
import { mcn } from '../pages/Config'
import { fmtPriceValue, isUnpriced, PRICE_UNIT } from '../lib/money'
import type { ModelInfo, ApiStoreItem } from '../lib/types'

const COST_CN: Record<string,string> = { budget:'省', standard:'标准', premium:'贵' }
const SPEED_CN: Record<string,string> = { fast:'快', medium:'中', slow:'慢' }

// 列宽契约。这张表以前**没有表头、也没有一处固定宽度** —— 9 个单元格里
// 8 处是自由宽度，还夹着 4 个条件列（未配key / 推荐阶段 / 能力标签 / 跑基准按钮），
// 于是每行的单元格数量都不一样，列自然对不齐。
// 现在：名字 flex 吸收剩余，其余全部定宽 + flexShrink:0（宁可横向滚动）。
const MC = {
  dot:   { width: 14,  flexShrink: 0 } as const,
  name:  { flex: 1, minWidth: 0 } as const,
  cost:  { width: 92,  flexShrink: 0 } as const,
  price: { width: 88,  flexShrink: 0, textAlign: 'right' as const },
  tags:  { width: 200, flexShrink: 0, display: 'flex', alignItems: 'center', gap: 4, overflow: 'hidden' } as const,
  act:   { width: 128, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 6 } as const,
}

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
  const [activeModels, setActiveModels] = useState<Set<string>>(new Set())
  const [observerModelId, setObserverModelId] = useState('')
  const [benchmarking, setBenchmarking] = useState('')
  const addToast = useToast()
  const modal = useModal()
  const run = useRun()

  const fetch = async () => {
    const [m, a, ag, obs] = await Promise.all([api.models(), api.apiStore() as Promise<ApiStoreItem[]>, api.agents(), api.observerModel()])
    setModels(m); setApis(a); setObserverModelId(obs as string)
    const ds = new Set<string>()
    for (const d of (ag?._disabled?.any||[])) ds.add(d)
    const act = new Set<string>()
    for (const lst of Object.values(ag||{})) {
      if (Array.isArray(lst)) for (const a of lst) if (a?.model && !ds.has(a.model)) act.add(a.model)
    }
    setDisabledSet(ds); setActiveModels(act)
  }
  useEffect(() => { fetch() }, [])

  /** 设置单价。存 model_prices.json，不动模型表 —— 跑基准/扫描导入擦不掉它。
   *  留空 = 清除（回到"未配置价格"），不是设成 0。 */
  const editPrice = (m: ModelInfo) => {
    let draft = m.price_per_m != null ? String(m.price_per_m) : ''
    modal.confirm({
      title: `「${mcn(m)}」的单价`,
      content: (
        <div>
          <div className="fs-11 text-muted" style={{ marginBottom: 6 }}>
            单位：USD / 百万 token（混合价 —— 系统只记总 token，不区分输入/输出）。<br />
            留空 = 不配置，用量页会如实显示「未配置价格」。
          </div>
          <Input defaultValue={draft} placeholder="例如 0.28" autoFocus
            onChange={e => { draft = e.target.value }} />
        </div>
      ),
      okText: '保存', cancelText: '取消',
      onOk: async () => {
        const t = draft.trim()
        const next = t === '' ? null : Number(t)
        if (next !== null && !Number.isFinite(next)) {
          addToast('单价必须是数字', 'error')
          return Promise.reject(new Error('invalid price'))
        }
        // 失败时 reject → 弹窗不关，用户输的值还在
        if (!(await run(() => api.setModelPrice(m.id, next)))) {
          return Promise.reject(new Error('save failed'))
        }
        fetch()
      },
    })
  }

  const scan = async (apiId: string) => {
    setScanning(apiId)
    try { setScanResults(await api.scanApiStore(apiId) as any) } catch (e) { setScanResults({error:String(e)}) }
    setScanning('')
  }

  const importSelected = async () => {
    const toImport = (scanResults?.models||[]).filter(m=>selected.has(m.id))
    if (!toImport.length) return
    if (!(await run(() => api.importModels(toImport)))) return
    setScanResults(null); fetch()
  }

  const runBenchmark = async (id: string) => {
    setBenchmarking(id)
    try { const r: any = await api.benchmarkModel(id); addToast(`基准完成: ${r.rating} · ${r.passed}/${r.total} 通过`, 'success'); await fetch() }
    catch (e) { addToast(String(e), 'error') }
    finally { setBenchmarking('') }
  }

  const pickProvider = (v: string) => {
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
            <Select size="small" style={{ width: 150 }} value={apiForm.mode} onChange={pickProvider}
              options={[{ value: '', label: '选择厂家…' }, ...PROVIDERS.map(p => ({ value: p.id, label: p.provider })), { value: '__custom__', label: '自定义…' }]}/>
            {apiForm.mode === '__custom__' && (
              <>
                <Input size="small" style={{ width: 110 }} placeholder="标识" value={apiForm.id} onChange={e=>setApiForm({...apiForm,id:e.target.value})}/>
                <Input size="small" style={{ width: 110 }} placeholder="提供商" value={apiForm.provider} onChange={e=>setApiForm({...apiForm,provider:e.target.value})}/>
                <Input size="small" style={{ width: 200 }} placeholder="基础URL" value={apiForm.base_url} onChange={e=>setApiForm({...apiForm,base_url:e.target.value})}/>
                <Input size="small" style={{ width: 150 }} placeholder="API密钥环境变量" value={apiForm.api_key_env} onChange={e=>setApiForm({...apiForm,api_key_env:e.target.value})}/>
              </>
            )}
            <Input.Password size="small" style={{ width: 220 }} placeholder="API Key（明文，写入 .env）" value={apiForm.api_key} onChange={e=>setApiForm({...apiForm,api_key:e.target.value})}/>
            <Button size="small" type="primary" onClick={addApi}>添加</Button>
          </div>
        )}
        <div className="flex-center gap-6 flex-wrap">
          {apis.map(a => (
            <div key={a.id} className="flex-center gap-6" style={{ padding: '4px 10px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', fontSize: 11 }}>
              <span className="fw-600">{a.provider||a.id}</span>
              <span className="fs-10" style={{ color: a.status==='active'?'var(--accent-green)':'var(--text-muted)' }}>{a.status==='active'?'●':'○'}</span>
              <button onClick={()=>scan(a.id)} disabled={scanning===a.id} className="btn-sm"><Search size={10}/> {scanning===a.id?'扫描中':'扫描'}</button>
              <button onClick={()=>modal.confirm({
                title: `删除 API 连接「${a.provider||a.id}」？`,
                content: '会同时移除 .env 里的 key，需要重新填写。',
                okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
                onOk: async () => { if (await run(() => api.deleteApiStore(a.id))) fetch() },
              })} className="btn-ghost-danger" aria-label={`删除 ${a.provider||a.id}`}><Trash2 size={10}/></button>
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
            <button onClick={()=>setScanResults(null)} className="btn-icon" aria-label="关闭扫描结果"><X size={14}/></button>
          </div>
          {scanResults.models && (
            <div className="flex-center gap-4 flex-wrap">
              {scanResults.models.map(m => (
                <label key={m.id} className="flex-center gap-4 fs-11" style={{ padding: '3px 8px', background: selected.has(m.id)?'var(--bg-tertiary)':'transparent', borderRadius: 4, cursor: 'pointer' }}>
                  <input type="checkbox" checked={selected.has(m.id)} onChange={()=>{const n=new Set(selected);n.has(m.id)?n.delete(m.id):n.add(m.id);setSelected(n)}}/>
                  {/* 走统一的 mcn —— 这里原来是 `m.display||m.id`，是第三种写法。
                      原始 id 留 title，要复制的时候悬停就有。 */}
                  <span title={m.id}>{mcn(m)}</span>
                  <span className="fs-10 text-muted">{m.rating}</span>
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
          <Select size="small" style={{ width: 170 }} value={observerModelId}
            onChange={(v) => { api.setObserverModel(v); setObserverModelId(v) }}
            options={[{ value: '', label: '未设置' }, ...models.map(m => ({ value: m.id, label: mcn(m) }))]}/>
        </div>
        {/* 表头 —— 以前没有。没有表头就没有列宽契约，条件列一多就各显示各的 */}
        <div className="card-row" style={{ ...MC, fontSize: 10, color: 'var(--text-muted)' } as any}>
          <span style={MC.dot} />
          <span style={MC.name}>模型</span>
          <span style={MC.cost}>成本·速度</span>
          <span style={MC.price}>单价{PRICE_UNIT}</span>
          <span style={MC.tags}>状态 / 推荐阶段 / 能力</span>
          <span style={MC.act} />
        </div>

        {models.map(m => {
          const rf = m.recommended_for||[]
          const disabled = disabledSet.has(m.id)
          const active = activeModels.has(m.id)
          const dotColor = active ? 'var(--accent-green)' : 'var(--text-muted)'
          return (
            <div key={m.id} className="card-row" style={{ ...MC, opacity: disabled?0.5:1 } as any}>
              <span style={{ ...MC.dot, color: dotColor, fontSize: 8 }}
                title={active ? '在调度阵容里' : '不在阵容里'}>{disabled?'○':'●'}</span>
              <span className="fw-500 truncate" style={MC.name}>{mcn(m)}{m.rating && m.rating !== '?' ? <span className="fs-10 text-muted" style={{marginLeft:6}}>{m.rating}</span> : <span className="card-tag" style={{marginLeft:6,opacity:.55}}>未评测</span>}</span>
              <span className="fs-10 text-muted" style={MC.cost}>{COST_CN[m.cost||'']||m.cost} · {SPEED_CN[m.speed||'']||m.speed}</span>
              {/* 单价入口。未配置时用警示色 —— 没配单价的模型在用量页算不出费用。
                  数值不带单位：单位在表头（和用量页一致）。 */}
              <span className="fs-10 mono" role="button" tabIndex={0}
                title="点击设置单价（USD / 百万 token，混合价）"
                onClick={() => editPrice(m)}
                onKeyDown={e => { if (e.key === 'Enter') editPrice(m) }}
                style={{ ...MC.price, cursor: 'pointer',
                  color: isUnpriced(m.price_per_m) ? '#d97706' : 'var(--text-secondary)' }}>
                {isUnpriced(m.price_per_m) ? '—' : fmtPriceValue(m.price_per_m)}
              </span>
              <span style={MC.tags}>
                {!m.api_available && <span className="fs-10" style={{color:'#d97706', flexShrink:0}}>未配key</span>}
                {rf.length > 0 && !(rf.length === 1 && rf[0] === 'any') && (
                  <>{rf.slice(0,3).map(p=><span key={p} className="card-tag">{p}</span>)}</>
                )}
                {(m.strengths||[]).length > 0 && (
                  <>{(m.strengths||[]).slice(0,2).map(s=><span key={s} className="card-tag">{s}</span>)}</>
                )}
              </span>
              <span style={MC.act}>
                {m.api_available && (
                  <button onClick={()=>runBenchmark(m.id)} disabled={benchmarking===m.id} className="btn-sm">
                    {benchmarking===m.id?'评测中…':'跑基准'}
                  </button>
                )}
                <button onClick={()=>modal.confirm({
                  title: `删除模型「${mcn(m)}」？`,
                  okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
                  onOk: async () => { if (await run(() => api.deleteModel(m.id))) fetch() },
                })} className="btn-ghost-danger" aria-label={`删除模型 ${m.id}`}><Trash2 size={10}/></button>
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
