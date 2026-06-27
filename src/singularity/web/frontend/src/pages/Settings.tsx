import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Settings2, Server, Key, Coins, Plus, Trash2, RefreshCw } from 'lucide-react'

type Tab = 'fusion' | 'apistore' | 'token'

export default function Settings() {
  const [tab, setTab] = useState<Tab>('fusion')
  const [fusion, setFusion] = useState<any>(null)
  const [apiStore, setApiStore] = useState<any[]>([])
  const [tokens, setTokens] = useState<any>(null)
  const [budget, setBudget] = useState('')

  useEffect(() => {
    if (tab === 'fusion') api.fusionConfig().then(setFusion)
    if (tab === 'apistore') api.apiStore().then(setApiStore)
    if (tab === 'token') api.tokenUsage().then(setTokens)
  }, [tab])

  const tabs = [
    { key: 'fusion' as Tab, label: 'Fusion 配置', icon: Settings2 },
    { key: 'apistore' as Tab, label: 'API Store', icon: Key },
    { key: 'token' as Tab, label: 'Token 预算', icon: Coins },
  ]

  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:14}}>系统配置</h2>
      
      <div style={{display:'flex',gap:6,marginBottom:14}}>
        {tabs.map(t=>(
          <button key={t.key} onClick={()=>setTab(t.key)}
            style={{background:tab===t.key?'var(--accent)':'var(--bg-secondary)',color:tab===t.key?'#fff':'var(--text-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:'6px 12px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:4}}>
            <t.icon size={14}/> {t.label}</button>
        ))}
      </div>

      {tab === 'fusion' && fusion && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
          <div style={{fontSize:13,fontWeight:600,marginBottom:8}}>Fusion 配置 (多模型碰撞)</div>
          {Object.entries(fusion).map(([tier, cfg]: [string, any]) => (
            <div key={tier} style={{marginBottom:8,padding:8,background:'var(--bg-tertiary)',borderRadius:4}}>
              <div style={{fontSize:12,fontWeight:600,color:'var(--accent)'}}>{tier}</div>
              <pre style={{fontSize:10,color:'var(--text-secondary)',margin:0}}>{JSON.stringify(cfg, null, 2)}</pre>
            </div>
          ))}
        </div>
      )}

      {tab === 'apistore' && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
          <div style={{fontSize:13,fontWeight:600,marginBottom:8}}>API Store ({apiStore.length})</div>
          {apiStore.map((a:any)=>(
            <div key={a.id} style={{display:'flex',justifyContent:'space-between',alignItems:'center',padding:'8px 10px',background:'var(--bg-tertiary)',borderRadius:4,marginBottom:4}}>
              <div>
                <span style={{fontWeight:600,fontSize:13}}>{a.provider||a.id}</span>
                <span style={{fontSize:11,color:'var(--text-secondary)',marginLeft:8}}>{a.base_url?.slice(0,40)}</span>
              </div>
              <div style={{display:'flex',gap:4,alignItems:'center'}}>
                <span style={{fontSize:10,color:a.status==='active'?'var(--accent-green)':'var(--text-muted)'}}>{a.status||'?'}</span>
                <button onClick={()=>api.scanApiStore(a.id).then(()=>api.apiStore().then(setApiStore))} style={{background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2}}><RefreshCw size={12}/></button>
                <button onClick={()=>api.deleteApiStore(a.id).then(()=>api.apiStore().then(setApiStore))} style={{background:'none',border:'none',color:'var(--text-muted)',cursor:'pointer',padding:2}}><Trash2 size={12}/></button>
              </div>
            </div>
          ))}
        </div>
      )}

      {tab === 'token' && tokens && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
          <div style={{fontSize:13,fontWeight:600,marginBottom:8}}>Token 用量</div>
          <pre style={{fontSize:11,color:'var(--text-secondary)',margin:0}}>{JSON.stringify(tokens, null, 2)}</pre>
          <div style={{display:'flex',gap:8,marginTop:10}}>
            <input placeholder="新预算" value={budget} onChange={e=>setBudget(e.target.value)}
              style={{background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:4,padding:'6px 8px',color:'var(--text-primary)',fontSize:12,width:120}}/>
            <button onClick={()=>api.updateTokenBudget({budget:parseInt(budget)}).then(()=>api.tokenUsage().then(setTokens))}
              style={{background:'var(--accent)',color:'#fff',border:'none',borderRadius:4,padding:'6px 12px',cursor:'pointer',fontSize:12}}>更新预算</button>
          </div>
        </div>
      )}
    </div>
  )
}
