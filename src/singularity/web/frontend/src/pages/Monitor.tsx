import { useState, useEffect } from 'react'
import { useSSE } from '../lib/useSSE'
import { Activity } from 'lucide-react'

interface LogEntry { ts: number; kind: string; text: string; color: string }

export default function Monitor() {
  const [events, setEvents] = useState<LogEntry[]>([])
  const [paused, setPaused] = useState(false)

  useSSE((event) => {
    if (paused) return
    if (event.kind === 'init') {
      addEvt('init', 'SSE 已连接', 'var(--accent-green)')
    } else if (event.kind === 'task_start') addEvt('task_start', (event.msg||'').slice(0,80), 'var(--accent)')
    else if (event.kind === 'task_done') addEvt('task_done', (event.msg||'').slice(0,80), 'var(--accent-green)')
    else if (event.kind === 'task_fail') addEvt('task_fail', (event.msg||'').slice(0,80), 'var(--accent-red)')
    else if (event.kind === 'task_cancel') addEvt('task_cancel', (event.msg||'').slice(0,80), 'var(--accent-yellow)')
    else if (event.kind === 'system') addEvt('system', (event.msg||'').slice(0,80), 'var(--text-muted)')
    else if (event.kind === 'idle') addEvt('idle', '队列空闲', 'var(--text-muted)')
    else if (event.kind !== 'ping' && event.kind !== 'heartbeat') addEvt(event.kind, JSON.stringify(event).slice(0,100), 'var(--text-secondary)')
  })

  const addEvt = (kind: string, text: string, color: string) => {
    setEvents(prev => [{ ts: Date.now(), kind, text, color }, ...prev].slice(0, 200))
  }

  const KIND_COLORS: Record<string,string> = {
    task_start: 'var(--accent)', task_done: 'var(--accent-green)',
    task_fail: 'var(--accent-red)', task_cancel: 'var(--accent-yellow)',
    system: 'var(--text-muted)', idle: 'var(--text-muted)', init: 'var(--accent-green)',
  }

  return (
    <div>
      <div style={{display:'flex',alignItems:'center',marginBottom:14}}>
        <h2 style={{fontSize:16,fontWeight:600,display:'flex',alignItems:'center',gap:6}}>
          <Activity size={16} color='var(--accent)'/> 实时监控</h2>
        <span style={{marginLeft:8,fontSize:12,color:'var(--text-muted)'}}>{events.length} 条事件</span>
        <button onClick={()=>{setPaused(!paused);if(paused)setEvents([])}}
          style={{marginLeft:'auto',background:paused?'var(--accent-green)':'var(--bg-tertiary)',color:paused?'#fff':'var(--text-secondary)',border:'1px solid var(--border)',borderRadius:4,padding:'4px 10px',cursor:'pointer',fontSize:11}}>
          {paused?'▶ 恢复':'⏸ 暂停'}</button>
      </div>

      <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',overflow:'hidden'}}>
        <div style={{fontSize:11,fontFamily:'var(--font-mono)',lineHeight:2.2,maxHeight:'calc(100vh - 140px)',overflow:'auto',padding:'8px 14px'}}>
          {events.length===0 && <span style={{color:'var(--text-muted)'}}>等待事件...</span>}
          {events.map((e,i)=><div key={i} style={{borderBottom:'1px solid var(--border)',padding:'3px 0',display:'flex',gap:10,alignItems:'baseline'}}>
            <span style={{color:'var(--text-muted)',fontSize:9,whiteSpace:'nowrap',minWidth:70}}>{new Date(e.ts).toLocaleTimeString()}</span>
            <span style={{color:KIND_COLORS[e.kind]||'var(--text-secondary)',fontSize:9,fontWeight:600,minWidth:80}}>{e.kind}</span>
            <span style={{color:e.color||'var(--text-primary)',wordBreak:'break-all'}}>{e.text}</span>
          </div>)}
        </div>
      </div>
    </div>
  )
}
