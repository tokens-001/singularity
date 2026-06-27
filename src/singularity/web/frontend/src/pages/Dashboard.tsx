import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { Play, Square } from 'lucide-react'

export default function Dashboard() {
  const [status, setStatus] = useState<any>(null)
  const [loop, setLoop] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [lastEvent, setLastEvent] = useState('')

  const refresh = () => {
    Promise.all([api.status(), api.loopStatus()]).then(([s, l]) => {
      setStatus(s); setLoop(l); setLoading(false)
    }).catch(() => setLoading(false))
  }

  useEffect(() => { refresh() }, [])

  useSSE((event) => {
    if (event.kind === 'init' && event.counts) {
      setStatus((prev: any) => prev ? { ...prev, counts: event.counts } : prev)
    } else if (event.kind === 'task_start') setLastEvent(`任务启动: ${(event.msg||'').slice(0,40)}`)
    else if (event.kind === 'task_done') setLastEvent(`任务完成: ${(event.msg||'').slice(0,40)}`)
    else if (event.kind !== 'ping' && event.kind !== 'heartbeat') refresh()
  })

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  const counts = status?.counts || {}
  const agents = status?.agents || {}

  const statCards = [
    { label: '总任务', v: Object.values(counts).reduce((a:any,b:any)=>a+b,0), color: '#e6edf3' },
    { label: '运行中', v: counts.running||0, color: '#58a6ff' },
    { label: '已完成', v: counts.done||0, color: '#3fb950' },
    { label: '失败', v: counts.failed||0, color: '#f85149' },
    { label: '已暂停', v: counts.blocked||0, color: '#d2991d' },
  ]

  return (
    <div style={{maxWidth:800}}>
      <h2 style={{fontSize:18,fontWeight:600,marginBottom:20}}>总览</h2>

      {/* Stats row */}
      <div style={{display:'flex',gap:16,marginBottom:28}}>
        {statCards.map(s => (
          <div key={s.label} style={{textAlign:'center'}}>
            <div style={{fontSize:36,fontWeight:700,color:s.color,lineHeight:1}}>{s.v}</div>
            <div style={{fontSize:12,color:'var(--text-secondary)',marginTop:4}}>{s.label}</div>
          </div>
        ))}
      </div>

      {/* Agent tiers */}
      <div style={{marginBottom:28}}>
        <div style={{fontSize:13,fontWeight:600,marginBottom:10,color:'var(--text-secondary)'}}>Agent</div>
        <div style={{display:'flex',gap:24}}>
          {['D','E+','E'].map(lvl => {
            const list = agents?.[lvl] || []
            const c = lvl==='D'?'#f0883e':lvl==='E+'?'#a371f7':'#58a6ff'
            return (
              <div key={lvl}>
                <div style={{fontSize:12,fontWeight:600,marginBottom:6,color:c}}>{lvl} 层 · {list.length}</div>
                {list.map((a:any) => <div key={a.model} style={{fontSize:11,color:'var(--text-primary)',fontFamily:'var(--font-mono)',marginBottom:2}}>{a.model}</div>)}
              </div>
            )
          })}
        </div>
      </div>

      {/* Loop + last event */}
      <div style={{display:'flex',alignItems:'center',gap:16,padding:'12px 16px',background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)'}}>
        <span style={{fontSize:13,fontWeight:600}}>调度</span>
        <span style={{fontSize:12,color:loop?.running?'var(--accent-green)':'var(--text-muted)'}}>{loop?.running?'运行中':'已停止'}</span>
        <button onClick={()=>(loop?.running?api.stopLoop():api.startLoop()).then(()=>location.reload())}
          style={{background:loop?.running?'var(--accent-red)':'var(--accent-green)',color:'#fff',border:'none',borderRadius:4,padding:'4px 14px',cursor:'pointer',fontSize:12}}>
          {loop?.running?'停止':'启动'}</button>
        {lastEvent && <span style={{marginLeft:'auto',fontSize:11,color:'var(--text-muted)',fontFamily:'var(--font-mono)'}}>{lastEvent}</span>}
      </div>
    </div>
  )
}
