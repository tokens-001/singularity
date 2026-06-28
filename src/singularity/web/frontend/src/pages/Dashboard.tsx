import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { Play, Square, Activity, Zap, AlertCircle, CheckCircle, Clock } from 'lucide-react'

export default function Dashboard() {
  const [status, setStatus] = useState<any>(null)
  const [loop, setLoop] = useState<any>(null)
  const [tokens, setTokens] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  const refresh = () => {
    Promise.all([api.status(), api.loopStatus(), api.tokenUsage()]).then(([s, l, t]) => {
      setStatus(s); setLoop(l); setTokens(t); setLoading(false)
    }).catch(() => setLoading(false))
  }

  useEffect(() => { refresh() }, [])

  // SSE 实时更新
  useSSE((event) => {
    if (event.kind === 'init' && event.counts) {
      setStatus((prev: any) => prev ? { ...prev, counts: event.counts } : prev)
    } else if (event.kind !== 'ping' && event.kind !== 'heartbeat') {
      refresh()
    }
  })

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  const counts = status?.counts || {}
  const agents = status?.agents || {}
  const totalAgents = Object.values(agents).reduce((sum: number, a: any) => sum + (a?.length || 0), 0)

  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:14}}>调度指挥中心</h2>

      {/* Stats */}
      <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(150px,1fr))',gap:10,marginBottom:16}}>
        {[
          {label:'总任务',v:Object.values(counts).reduce((a:any,b:any)=>a+b,0),icon:Activity,color:'var(--text-primary)'},
          {label:'运行中',v:counts.running||0,icon:Play,color:'var(--accent)'},
          {label:'已完成',v:counts.done||0,icon:CheckCircle,color:'var(--accent-green)'},
          {label:'失败',v:counts.failed||0,icon:AlertCircle,color:'var(--accent-red)'},
          {label:'已暂停',v:counts.blocked||0,icon:Clock,color:'var(--accent-yellow)'},
          {label:'Agent',v:totalAgents,icon:Zap,color:'var(--accent-purple)'},
        ].map(s=>(
          <div key={s.label} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:'12px 14px'}}>
            <s.icon size={18} color={s.color} style={{marginBottom:6}}/>
            <div style={{fontSize:24,fontWeight:700,color:s.color}}>{s.v}</div>
            <div style={{fontSize:11,color:'var(--text-secondary)'}}>{s.label}</div>
          </div>
        ))}
      </div>

      {/* Agent 池 */}
      <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(200px,1fr))',gap:10,marginBottom:16}}>
        {['D','E+','E'].map(lvl=>{
          const list = agents?.[lvl] || []
          return (
            <div key={lvl} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:12}}>
              <div style={{fontSize:13,fontWeight:600,marginBottom:8,color:'var(--text-secondary)'}}>{lvl} 层 ({list.length})</div>
              {list.slice(0,5).map((a:any)=><div key={a.model} style={{fontSize:12,color:'var(--text-primary)',fontFamily:'var(--font-mono)',marginBottom:2}}>{a.model}</div>)}
            </div>
          )
        })}
      </div>

      {/* Loop control */}
      <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:'12px 14px',display:'flex',alignItems:'center',gap:12}}>
        <span style={{fontSize:13,fontWeight:600}}>调度循环</span>
        <span style={{fontSize:12,color:loop?.running?'var(--accent-green)':'var(--text-muted)'}}>{loop?.running?'运行中':'已停止'}</span>
        {loop?.running ? (
          <button onClick={()=>api.stopLoop().then(()=>location.reload())} style={{background:'var(--accent-red)',color:'#fff',border:'none',borderRadius:4,padding:'4px 12px',cursor:'pointer',fontSize:12,marginLeft:'auto'}}><Square size={12}/> 停止</button>
        ) : (
          <button onClick={()=>api.startLoop().then(()=>location.reload())} style={{background:'var(--accent-green)',color:'#fff',border:'none',borderRadius:4,padding:'4px 12px',cursor:'pointer',fontSize:12,marginLeft:'auto'}}><Play size={12}/> 启动</button>
        )}
      </div>
    </div>
  )
}
