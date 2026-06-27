import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Plus, Trash2, RefreshCw } from 'lucide-react'

export default function AgentDashboard() {
  const [agents, setAgents] = useState<any>({})
  const [loading, setLoading] = useState(true)

  const fetch = () => { setLoading(true); api.agents().then(setAgents).finally(()=>setLoading(false)) }
  useEffect(() => { fetch() }, [])

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:14}}>Agent 注册表</h2>
      {['D','E+','E'].map(lvl=>{
        const list = agents?.[lvl] || []
        return (
          <div key={lvl} style={{marginBottom:16}}>
            <div style={{fontSize:14,fontWeight:600,marginBottom:8,color:'var(--text-secondary)'}}>{lvl} 层 ({list.length})</div>
            <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(280px,1fr))',gap:10}}>
              {list.map((a:any)=>(
                <div key={a.model} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
                  <div style={{display:'flex',justifyContent:'space-between',alignItems:'start'}}>
                    <div>
                      <div style={{fontWeight:600,fontFamily:'var(--font-mono)',fontSize:13}}>{a.model}</div>
                      <div style={{fontSize:11,color:'var(--text-secondary)',marginTop:2}}>{a.type} {a.default ? '(默认)' : ''}</div>
                    </div>
                    <button onClick={()=>api.deleteAgent(lvl,a.model).then(fetch)} style={{background:'none',border:'none',color:'var(--text-muted)',cursor:'pointer',padding:2}}><Trash2 size={14}/></button>
                  </div>
                  {a.roles && <div style={{display:'flex',gap:4,marginTop:6,flexWrap:'wrap'}}>{a.roles.map((r:string)=><span key={r} style={{background:'var(--bg-tertiary)',color:'var(--text-secondary)',padding:'2px 6px',borderRadius:3,fontSize:10}}>{r}</span>)}</div>}
                  <div style={{fontSize:10,color:'var(--text-muted)',marginTop:6,fontFamily:'var(--font-mono)',wordBreak:'break-all'}}>{a.entry?.slice(0,60)}</div>
                </div>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}
