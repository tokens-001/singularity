import { useState, useEffect, useRef } from 'react'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { Play, Square, Activity, Zap, AlertCircle, CheckCircle, Clock } from 'lucide-react'

interface LogEntry { ts: number; text: string; color: string }

export default function Dashboard() {
  const [status, setStatus] = useState<any>(null)
  const [loop, setLoop] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [events, setEvents] = useState<LogEntry[]>([])
  const logRef = useRef<HTMLDivElement>(null)
  const MAX_EVENTS = 50

  const addEvent = (text: string, color = 'var(--text-secondary)') => {
    setEvents(prev => [{ ts: Date.now(), text, color }, ...prev].slice(0, MAX_EVENTS))
  }

  const refresh = () => {
    Promise.all([api.status(), api.loopStatus()]).then(([s, l]) => {
      setStatus(s); setLoop(l); setLoading(false)
    }).catch(() => setLoading(false))
  }

  useEffect(() => { refresh() }, [])

  useSSE((event) => {
    if (event.kind === 'init' && event.counts) {
      setStatus((prev: any) => prev ? { ...prev, counts: event.counts } : prev)
      addEvent('SSE 已连接', 'var(--accent-green)')
    } else if (event.kind === 'task_start') {
      addEvent(`任务启动: ${(event.msg||'').slice(0,60)}`, 'var(--accent)')
    } else if (event.kind === 'task_done') {
      addEvent(`任务完成: ${(event.msg||'').slice(0,60)}`, 'var(--accent-green)')
    } else if (event.kind === 'task_fail') {
      addEvent(`任务失败: ${(event.msg||'').slice(0,60)}`, 'var(--accent-red)')
    } else if (event.kind === 'task_cancel') {
      addEvent(`任务取消: ${(event.msg||'').slice(0,60)}`, 'var(--accent-yellow)')
    } else if (event.kind === 'system') {
      addEvent(`系统: ${(event.msg||'').slice(0,80)}`, 'var(--text-muted)')
    } else if (event.kind === 'idle') {
      addEvent('队列空闲，等待新任务...', 'var(--text-muted)')
    } else if (event.kind !== 'ping' && event.kind !== 'heartbeat') {
      refresh()
    }
  })

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  const counts = status?.counts || {}
  const agents = status?.agents || {}
  const totalAgents = Object.values(agents).reduce((sum: number, a: any) => sum + (a?.length || 0), 0)

  return (
    <div style={{display:'flex',gap:14,height:'calc(100vh - 80px)'}}>
      {/* Left: Stats + Agents */}
      <div style={{flex:1,overflow:'auto'}}>
        <h2 style={{fontSize:16,fontWeight:600,marginBottom:14}}>调度指挥中心</h2>

        <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(130px,1fr))',gap:8,marginBottom:14}}>
          {[
            {label:'总任务',v:Object.values(counts).reduce((a:any,b:any)=>a+b,0),icon:Activity,color:'var(--text-primary)'},
            {label:'运行中',v:counts.running||0,icon:Play,color:'var(--accent)'},
            {label:'已完成',v:counts.done||0,icon:CheckCircle,color:'var(--accent-green)'},
            {label:'失败',v:counts.failed||0,icon:AlertCircle,color:'var(--accent-red)'},
            {label:'已暂停',v:counts.blocked||0,icon:Clock,color:'var(--accent-yellow)'},
            {label:'Agent',v:totalAgents,icon:Zap,color:'var(--accent-purple)'},
          ].map(s=>(
            <div key={s.label} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:'10px 12px'}}>
              <s.icon size={16} color={s.color} style={{marginBottom:4}}/>
              <div style={{fontSize:22,fontWeight:700,color:s.color}}>{s.v}</div>
              <div style={{fontSize:10,color:'var(--text-secondary)'}}>{s.label}</div>
            </div>
          ))}
        </div>

        {['D','E+','E'].map(lvl=>{
          const list = agents?.[lvl] || []
          return (<div key={lvl} style={{marginBottom:10}}>
            <div style={{fontSize:12,fontWeight:600,marginBottom:4,color:'var(--text-secondary)'}}>{lvl} 层 ({list.length})</div>
            <div style={{display:'flex',flexWrap:'wrap',gap:4}}>
              {list.map((a:any)=><span key={a.model} style={{background:'var(--bg-tertiary)',padding:'3px 8px',borderRadius:3,fontSize:10,fontFamily:'var(--font-mono)',color:'var(--text-primary)'}}>{a.model}</span>)}
            </div>
          </div>)
        })}

        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:'10px 14px',display:'flex',alignItems:'center',gap:10,marginTop:10}}>
          <span style={{fontSize:12,fontWeight:600}}>调度循环</span>
          <span style={{fontSize:11,color:loop?.running?'var(--accent-green)':'var(--text-muted)'}}>{loop?.running?'运行中':'已停止'}</span>
          {loop?.running ? (
            <button onClick={()=>api.stopLoop().then(()=>location.reload())} style={{background:'var(--accent-red)',color:'#fff',border:'none',borderRadius:4,padding:'4px 10px',cursor:'pointer',fontSize:11,marginLeft:'auto'}}><Square size={12}/> 停止</button>
          ) : (
            <button onClick={()=>api.startLoop().then(()=>location.reload())} style={{background:'var(--accent-green)',color:'#fff',border:'none',borderRadius:4,padding:'4px 10px',cursor:'pointer',fontSize:11,marginLeft:'auto'}}><Play size={12}/> 启动</button>
          )}
        </div>
      </div>

      {/* Right: Live event stream */}
      <div style={{width:320,flexShrink:0,display:'flex',flexDirection:'column'}}>
        <div style={{fontSize:12,fontWeight:600,marginBottom:8,color:'var(--text-secondary)'}}>实时事件</div>
        <div ref={logRef} style={{flex:1,background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:8,overflow:'auto',fontSize:10,fontFamily:'var(--font-mono)',lineHeight:1.8}}>
          {events.length===0 && <span style={{color:'var(--text-muted)'}}>等待事件...</span>}
          {events.map((e,i)=><div key={i} style={{color:e.color,marginBottom:2,borderBottom:'1px solid var(--border)',paddingBottom:2}}>
            <span style={{color:'var(--text-muted)',fontSize:8}}>{new Date(e.ts).toLocaleTimeString()}</span> {e.text}
          </div>)}
        </div>
      </div>
    </div>
  )
}
