import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Cpu, Trash2, Plus, RefreshCw, Download, Search } from 'lucide-react'

export default function ModelManagement() {
  const [models, setModels] = useState<any[]>([])
  const [apiStore, setApiStore] = useState<any[]>([])
  const [scanResults, setScanResults] = useState<any>(null)
  const [scanning, setScanning] = useState('')
  const [tierFilter, setTierFilter] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const [addForm, setAddForm] = useState<any>({ id: '', provider: '', tier: '', context_window: '', cost_1k_input: '', cost_1k_output: '' })

  const fetch = async () => {
    const [m, a] = await Promise.all([api.models(), api.apiStore()])
    setModels(m); setApiStore(a)
  }
  useEffect(() => { fetch() }, [])

  const handleScan = async (apiId: string) => {
    setScanning(apiId)
    try {
      const result = await api.scanApiStore(apiId)
      setScanResults(Object.assign({ apiId }, result))
    } catch (e) { setScanResults({ apiId, error: String(e) }) }
    setScanning('')
  }

  const handleImport = async (modelsToImport: any[]) => {
    await api.importModels(modelsToImport)
    setScanResults(null); fetch()
  }

  const handleDelete = async (id: string) => {
    await api.deleteModel(id); fetch()
  }

  const filtered = tierFilter ? models.filter((m:any) => m.tier === tierFilter) : models
  const tiers = ['D', 'E+', 'E']

  return (
    <div>
      <div style={{display:'flex',alignItems:'center',marginBottom:14}}>
        <h2 style={{fontSize:16,fontWeight:600}}>模型管理 ({models.length})</h2>
        <div style={{display:'flex',gap:6,marginLeft:16}}>
          <button onClick={()=>setTierFilter('')} style={filterBtn(tierFilter==='')}>全部</button>
          {tiers.map(t=><button key={t} onClick={()=>setTierFilter(t)} style={filterBtn(tierFilter===t)}>{t} 层</button>)}
        </div>
        <button onClick={()=>setShowAdd(!showAdd)}
          style={{marginLeft:'auto',background:'var(--accent)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'6px 12px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:4}}>
          <Plus size={14}/> 手动添加</button>
      </div>

      {/* Add form */}
      {showAdd && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--accent)',borderRadius:'var(--radius)',padding:'10px 14px',marginBottom:14,display:'flex',alignItems:'center',gap:10,flexWrap:'wrap'}}>
          <input placeholder="模型 ID" value={addForm.id} onChange={e=>setAddForm({...addForm,id:e.target.value})} style={{...inp,width:160}}/>
          <input placeholder="Provider" value={addForm.provider} onChange={e=>setAddForm({...addForm,provider:e.target.value})} style={{...inp,width:100}}/>
          <select value={addForm.tier} onChange={e=>setAddForm({...addForm,tier:e.target.value})} style={{...inp,width:80}}><option value="">层级</option>{tiers.map(t=><option key={t}>{t}</option>)}</select>
          <input placeholder="上下文窗口" type="number" value={addForm.context_window} onChange={e=>setAddForm({...addForm,context_window:e.target.value})} style={{...inp,width:100}}/>
          <input placeholder="输入$/1k" type="number" step="0.001" value={addForm.cost_1k_input} onChange={e=>setAddForm({...addForm,cost_1k_input:e.target.value})} style={{...inp,width:90}}/>
          <input placeholder="输出$/1k" type="number" step="0.001" value={addForm.cost_1k_output} onChange={e=>setAddForm({...addForm,cost_1k_output:e.target.value})} style={{...inp,width:90}}/>
          <button onClick={async()=>{await api.addModel({...addForm,context_window:parseInt(addForm.context_window)||0,cost_1k_input:parseFloat(addForm.cost_1k_input)||0,cost_1k_output:parseFloat(addForm.cost_1k_output)||0});setAddForm({id:'',provider:'',tier:'',context_window:'',cost_1k_input:'',cost_1k_output:''});setShowAdd(false);fetch()}} style={{background:'var(--accent-green)',color:'#fff',border:'none',borderRadius:4,padding:'6px 12px',cursor:'pointer',fontSize:12}}>添加</button>
          <button onClick={()=>setShowAdd(false)} style={{background:'none',border:'none',color:'var(--text-muted)',cursor:'pointer',fontSize:16}}>✕</button>
        </div>
      )}

      {/* API scan section */}
      <div style={{marginBottom:16}}>
        <div style={{fontSize:12,color:'var(--text-muted)',marginBottom:8}}>从 API 扫描模型 — 先在 <a href="/settings" style={{color:'var(--accent)'}}>配置 → API Store</a> 添加 API 连接</div>
        <div style={{display:'flex',gap:8,flexWrap:'wrap'}}>
          {apiStore.map((a:any)=>(
            <div key={a.id} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:'8px 12px',display:'flex',alignItems:'center',gap:8}}>
              <span style={{fontSize:12,fontWeight:600}}>{a.provider||a.id}</span>
              <span style={{fontSize:10,color:a.status==='active'?'var(--accent-green)':'var(--text-muted)'}}>{a.status==='active'?'已连接':'未连接'}</span>
              <button onClick={()=>handleScan(a.id)} disabled={scanning===a.id}
                style={{background:'var(--accent)',color:'#fff',border:'none',borderRadius:4,padding:'3px 8px',cursor:'pointer',fontSize:11,display:'flex',alignItems:'center',gap:3}}>
                <Search size={12}/> {scanning===a.id?'扫描中...':'扫描模型'}</button>
            </div>
          ))}
        </div>
      </div>

      {/* Scan results */}
      {scanResults && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--accent)',borderRadius:'var(--radius)',padding:14,marginBottom:14}}>
          <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:8}}>
            <span style={{fontWeight:600,fontSize:13}}>
              {scanResults.error ? <span style={{color:'var(--accent-red)'}}>扫描失败: {scanResults.error}</span>
                : <>{scanResults.provider} — 发现 {scanResults.total} 个模型</>}
            </span>
            <div style={{display:'flex',gap:6}}>
              {scanResults.models && <button onClick={()=>handleImport(scanResults.models.map((m:any)=>(Object.assign({},m,{provider:scanResults.provider}))))} style={{background:'var(--accent-green)',color:'#fff',border:'none',borderRadius:4,padding:'4px 10px',cursor:'pointer',fontSize:11,display:'flex',alignItems:'center',gap:3}}><Download size={12}/> 全部导入</button>}
              <button onClick={()=>setScanResults(null)} style={{background:'none',border:'none',color:'var(--text-muted)',cursor:'pointer',fontSize:16}}>✕</button>
            </div>
          </div>
          {scanResults.models && (
            <div style={{display:'flex',flexWrap:'wrap',gap:4}}>
              {scanResults.models.map((m:any)=>(
                <span key={m.id} style={{background:'var(--bg-tertiary)',padding:'3px 8px',borderRadius:3,fontSize:11,fontFamily:'var(--font-mono)',color:'var(--text-primary)'}}>
                  {m.id} {m.display && <span style={{color:'var(--text-muted)'}}>({m.display})</span>}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Model list */}
      <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',overflow:'hidden'}}>
        <table style={{width:'100%',borderCollapse:'collapse'}}>
          <thead><tr style={{borderBottom:'1px solid var(--border)',fontSize:11,color:'var(--text-muted)',textAlign:'left'}}>
            <th style={{padding:'7px 10px',width:180}}>模型</th>
            <th style={{padding:'7px 10px',width:80}}>来源</th>
            <th style={{padding:'7px 10px',width:60}}>层级</th>
            <th style={{padding:'7px 10px',width:90}}>上下文</th>
            <th style={{padding:'7px 10px',width:120}}>成本 (/1k)</th>
            <th style={{padding:'7px 10px',width:50}}></th>
          </tr></thead>
          <tbody>
            {filtered.map((m:any)=>(
              <tr key={m.id||m.model} style={{borderBottom:'1px solid var(--border)',fontSize:12}}>
                <td style={{padding:'7px 10px',fontFamily:'var(--font-mono)',fontSize:12}}>
                  <Cpu size={12} color='var(--accent)' style={{display:'inline',marginRight:6,verticalAlign:'middle'}}/>{m.id||m.model}</td>
                <td style={{padding:'7px 10px',color:'var(--text-secondary)'}}>{m.provider||'-'}</td>
                <td style={{padding:'7px 10px'}}>
                  {m.tier ? <span style={{background:'var(--bg-tertiary)',padding:'1px 6px',borderRadius:3,fontSize:10,fontWeight:600,color:m.tier==='D'?'#f0883e':m.tier==='E+'?'#a371f7':'#58a6ff'}}>{m.tier}</span> : '-'}
                </td>
                <td style={{padding:'7px 10px',color:'var(--text-secondary)',fontFamily:'var(--font-mono)',fontSize:11}}>{m.context_window ? m.context_window.toLocaleString() : '-'}</td>
                <td style={{padding:'7px 10px',color:'var(--text-secondary)',fontSize:10}}>{m.cost_1k_input ? <>${m.cost_1k_input} / ${m.cost_1k_output}</> : '-'}</td>
                <td style={{padding:'7px 10px'}}>
                  <button onClick={()=>handleDelete(m.id||m.model)} style={{background:'none',border:'none',color:'var(--accent-red)',cursor:'pointer',padding:2}}><Trash2 size={12}/></button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

const inp = { background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:3,padding:'3px 6px',color:'var(--text-primary)',fontSize:11 } as const
const filterBtn = (active: boolean) => ({ background:active?'var(--accent)':'var(--bg-tertiary)',color:active?'#fff':'var(--text-secondary)',border:'1px solid var(--border)',borderRadius:4,padding:'3px 10px',cursor:'pointer',fontSize:11 })
