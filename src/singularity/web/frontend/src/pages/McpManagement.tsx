import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Server, Plug, RefreshCw, Wrench } from 'lucide-react'

export default function McpManagement() {
  const [servers, setServers] = useState<any[]>([])
  const [tools, setTools] = useState<any[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([api.mcpServers(), api.mcpTools()]).then(([s,t]) => {
      setServers(s); setTools(t); setLoading(false)
    }).catch(() => setLoading(false))
  }, [])

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:14}}>MCP 管理</h2>

      {/* Servers */}
      <div style={{marginBottom:16}}>
        <div style={{fontSize:14,fontWeight:600,marginBottom:8,display:'flex',alignItems:'center',gap:6}}><Server size={16}/> 服务器 ({servers.length})</div>
        <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(300px,1fr))',gap:10}}>
          {servers.map(s=>(
            <div key={s.name} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
              <div style={{display:'flex',justifyContent:'space-between',alignItems:'center'}}>
                <div style={{fontWeight:600,fontSize:13}}>{s.name}</div>
                <span style={{display:'flex',alignItems:'center',gap:4,fontSize:11,color:s.status==='connected'?'var(--accent-green)':'var(--text-muted)'}}>
                  <span style={{width:6,height:6,borderRadius:'50%',background:s.status==='connected'?'var(--accent-green)':'var(--text-muted)',display:'inline-block'}}/>{s.status}</span>
              </div>
              {s.url && <div style={{fontSize:10,color:'var(--text-muted)',marginTop:2,fontFamily:'var(--font-mono)'}}>{s.url}</div>}
            </div>
          ))}
        </div>
      </div>

      {/* Tools */}
      <div>
        <div style={{fontSize:14,fontWeight:600,marginBottom:8,display:'flex',alignItems:'center',gap:6}}><Wrench size={16}/> 工具 ({tools.length})</div>
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',overflow:'hidden'}}>
          <table style={{width:'100%',borderCollapse:'collapse'}}>
            <thead><tr style={{borderBottom:'1px solid var(--border)',fontSize:11,color:'var(--text-muted)',textAlign:'left'}}>
              <th style={{padding:'6px 10px'}}>工具名</th><th style={{padding:'6px 10px'}}>服务器</th><th style={{padding:'6px 10px'}}>描述</th>
            </tr></thead>
            <tbody>
              {tools.map((t,i)=>(
                <tr key={i} style={{borderBottom:'1px solid var(--border)',fontSize:12}}>
                  <td style={{padding:'6px 10px',fontFamily:'var(--font-mono)',fontSize:11}}>{t.name}</td>
                  <td style={{padding:'6px 10px',color:'var(--text-secondary)'}}>{t.server}</td>
                  <td style={{padding:'6px 10px',color:'var(--text-muted)',fontSize:11}}>{t.description?.slice(0,80)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
