import { useState, useRef, useEffect } from 'react'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { Send, Bot, User, Play, Square, RefreshCw } from 'lucide-react'

interface Msg { role: 'user' | 'assistant'; content: string }

export default function Chat() {
  const [msgs, setMsgs] = useState<Msg[]>([{role:'assistant',content:'你好！我是 Observer。可以问我系统状态、创建任务，或者直接说「帮我做一个xxx」开始。'}])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [status, setStatus] = useState<any>({})
  const [loopRunning, setLoopRunning] = useState(false)
  const [events, setEvents] = useState<any[]>([])
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => { refreshStatus() }, [])
  useEffect(() => { bottomRef.current?.scrollIntoView({behavior:'smooth'}) }, [msgs])

  useSSE((e: any) => {
    if (e.kind === 'init') { setLoopRunning(e.running); setStatus(e) }
    else if (e.kind === 'task' || e.kind === 'system') {
      setEvents((prev: any[]) => [e, ...prev].slice(0, 20))
      if (e.kind === 'task') refreshStatus()
    }
  })

  const refreshStatus = async () => {
    try { const s = await api.status(); setStatus(s); setLoopRunning(!!s.loop_running||!!s.running) } catch {}
  }

  const send = async () => {
    const q = input.trim(); if (!q || loading) return
    setMsgs(prev => [...prev, {role:'user',content:q}]); setInput(''); setLoading(true)
    try {
      const r = await api.observerChat(q)
      setMsgs(prev => [...prev, {role:'assistant',content:r.answer||r.text||r.reply||'(无回复)'}])
    } catch { setMsgs(prev => [...prev, {role:'assistant',content:'请求失败，请重试'}]) }
    setLoading(false)
  }

  const toggleLoop = () => loopRunning ? api.stopLoop().then(refreshStatus) : api.startLoop().then(refreshStatus)
  const counts = status?.counts || {}

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', maxWidth: 900, margin: '0 auto', width: '100%' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 10px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', marginBottom: 10, fontSize: 11, fontFamily: 'var(--font-mono)' }}>
        <span style={{ color: loopRunning ? 'var(--accent-green)' : 'var(--accent-red)' }}>{loopRunning ? '◆' : '◇'}</span>
        <span style={{ color: 'var(--text-secondary)' }}>任务: {counts.total||status?.running_total||0}</span>
        <span style={{ color: 'var(--accent)' }}>运行: {counts.running||0}</span>
        <span style={{ color: 'var(--accent-green)' }}>完成: {counts.done||0}</span>
        <span style={{ flex: 1 }} />
        <button onClick={refreshStatus} style={{background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2}}><RefreshCw size={12}/></button>
        <button onClick={toggleLoop} style={{background:'none',border:'none',color:loopRunning?'var(--accent-red)':'var(--accent-green)',cursor:'pointer',padding:2}}>
          {loopRunning ? <Square size={12}/> : <Play size={12}/>}
        </button>
      </div>
      <div style={{ flex: 1, overflow: 'auto', marginBottom: 8 }}>
        {msgs.map((m, i) => (
          <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 10, alignItems: 'flex-start' }}>
            <span style={{ marginTop: 2, color: m.role === 'user' ? 'var(--accent)' : 'var(--accent-green)' }}>
              {m.role === 'user' ? <User size={14}/> : <Bot size={14}/>}
            </span>
            <div style={{ flex: 1, fontSize: 13, color: 'var(--text-primary)', whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>{m.content}</div>
          </div>
        ))}
        {loading && <div style={{ color: 'var(--text-muted)', fontSize: 11, padding: 4 }}>思考中...</div>}
        <div ref={bottomRef} />
      </div>
      {events.length > 0 && (
        <div style={{ maxHeight: 100, overflow: 'auto', marginBottom: 8, borderTop: '1px solid var(--border)', paddingTop: 4 }}>
          {events.slice(0, 8).map((e: any, i: number) => (
            <div key={i} style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', padding: '1px 0' }}>
              {new Date(e.ts*1000).toLocaleTimeString('zh-CN')} {e.msg||''}
            </div>
          ))}
        </div>
      )}
      <div style={{ display: 'flex', gap: 6 }}>
        <input value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
          placeholder="问 Observer、或说「帮我做一个xxx」..."
          style={{ flex: 1, background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: '8px 12px', color: 'var(--text-primary)', fontSize: 13 }} />
        <button onClick={send} disabled={loading} style={{ background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: 'var(--radius)', padding: '8px 14px', cursor: 'pointer' }}>
          <Send size={14}/>
        </button>
      </div>
    </div>
  )
}
