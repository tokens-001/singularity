import { useState, useRef, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import { Send, Bot, User, Wifi, WifiOff } from 'lucide-react'

interface Msg { role: 'user' | 'assistant' | 'system'; content: string; ts?: number }

export default function ObserverChat() {
  const [msgs, setMsgs] = useState<Msg[]>([{role:'assistant',content:'你好！我是 Observer。可以问我系统状态、任务详情，或者帮你创建任务。'}])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [wsConnected, setWsConnected] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const retriesRef = useRef(0)
  const timeoutRef = useRef<number | null>(null)
  // ── WebSocket primary channel ──
  const connectWS = useCallback(() => {
    try { wsRef.current = new WebSocket('ws://127.0.0.1:8765') } catch { return }

    wsRef.current.onopen = () => {
      setWsConnected(true); retriesRef.current = 0
      wsRef.current!.send(JSON.stringify({action:'subscribe',channels:['tasks','system','alerts']}))
    }

    wsRef.current.onmessage = (e) => {
      try {
        const m = JSON.parse(e.data)

        // chat_answer
        if (m.event === 'chat_answer') {
          const text = (m.data||{}).text || ''
          setMsgs(prev => prev.filter(m=>m.role!=='system'||!m.content.startsWith('⏳')).concat([{ role: 'assistant', content: text }]))
          setLoading(false)
          return
        }

        // system events → compressed display
        const kindMap: Record<string,string> = { task: '📋', system: '⚙', idle: '⚙', workflow: '⚙', error: '⚠' }
        const emoji = kindMap[m.event] || '•'
        const label = m.event === 'error' ? 'var(--accent-red)' : m.event === 'task' ? 'var(--text-muted)' : 'var(--text-secondary)'
        const text = (m.data||{}).msg || ''
        if (text) setMsgs(prev => [...prev, { role: 'system', content: `${emoji} ${text}`, ts: Date.now() }])
      } catch {}
    }

    wsRef.current.onclose = () => {
      setWsConnected(false)
      if (retriesRef.current < 5) { retriesRef.current++; setTimeout(connectWS, 3000) }
    }

    wsRef.current.onerror = () => { setWsConnected(false) }
  }, [])

  // SSE fallback — only processes observer_answer when WS is down
  const handleSSE = useCallback((e: MessageEvent) => {
    if (wsConnected) return // WS active, skip SSE
    try {
      const d = JSON.parse(e.data)
      if (d.kind !== 'observer_answer') return
      const inner = JSON.parse(d.msg)
      const answer = inner.answer || inner.text || ''
      if (answer) {
        setMsgs(prev => prev.filter(m=>m.role!=='system'||!m.content.startsWith('⏳')).concat([{ role: 'assistant', content: answer }]))
        setLoading(false)
      }
    } catch {}
  }, [wsConnected])

  useEffect(() => {
    connectWS()
    const es = new EventSource('/api/events')
    es.onmessage = handleSSE
    return () => { es.close(); wsRef.current?.close() }
  }, [])

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [msgs])

  const send = async () => {
    if (!input.trim() || loading) return
    const q = input; setInput(''); setMsgs(prev=>[...prev,{role:'user',content:q}]); setLoading(true)

    // 1. WS primary
    if (wsConnected && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({action:'chat',question:q}))
      if (timeoutRef.current != null) clearTimeout(timeoutRef.current)
      timeoutRef.current = window.setTimeout(() => {
        if (loading) { setMsgs(prev=>[...prev,{role:'system',content:'✗ 超时，Observer 未响应'}]); setLoading(false) }
      }, 30000)
      return
    }

    // 2. HTTP POST → SSE fallback
    try {
      await api.observerChat(q)
      if (timeoutRef.current != null) clearTimeout(timeoutRef.current)
      timeoutRef.current = window.setTimeout(() => {
        if (loading) { setMsgs(prev=>[...prev,{role:'system',content:'✗ 超时，Observer 未响应'}]); setLoading(false) }
      }, 30000)
    } catch {
      setMsgs(prev=>[...prev,{role:'assistant',content:'Observer 暂时不可用'}])
      setLoading(false)
    }
  }

  const systemMsgStyle = (m: Msg) => ({
    color: m.content.startsWith('⚠') ? 'var(--accent-red)' : m.content.startsWith('✗') ? 'var(--accent-red)' : 'var(--text-muted)',
    fontSize: 10, fontFamily: 'var(--font-mono)', padding: '2px 14px'
  })

  return (
    <div style={{display:'flex',flexDirection:'column',height:'calc(100vh - 80px)'}}>
      {/* WS status indicator */}
      <div style={{display:'flex',alignItems:'center',gap:6,marginBottom:8,fontSize:10,color:'var(--text-muted)'}}>
        {wsConnected ? <><Wifi size={12} color='var(--accent-green)'/> WS 已连接</> : <><WifiOff size={12} color='var(--accent-red)'/> WS 离线 · SSE 后备</>}
      </div>

      <div style={{flex:1,overflow:'auto',marginBottom:12}}>
        {msgs.map((m,i)=>(
          m.role === 'system' ? (
            <div key={i} style={systemMsgStyle(m)}>{m.content}</div>
          ) : (
            <div key={i} style={{display:'flex',gap:8,marginBottom:12,justifyContent:m.role==='user'?'flex-end':'flex-start'}}>
              {m.role==='assistant' && <Bot size={20} color='var(--accent)' style={{marginTop:4,flexShrink:0}}/>}
              <div style={{maxWidth:'70%',background:m.role==='user'?'var(--accent)':'var(--bg-secondary)',color:m.role==='user'?'#fff':'var(--text-primary)',padding:'10px 14px',borderRadius:'var(--radius)',fontSize:13,lineHeight:1.6,whiteSpace:'pre-wrap'}}>
                {m.content}
              </div>
              {m.role==='user' && <User size={20} color='var(--text-muted)' style={{marginTop:4,flexShrink:0}}/>}
            </div>
          )
        ))}
        <div ref={bottomRef}/>
      </div>
      <div style={{display:'flex',gap:8}}>
        <input value={input} onChange={e=>setInput(e.target.value)} placeholder="询问系统状态或创建任务..."
          style={{flex:1,background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:'10px 14px',color:'var(--text-primary)',fontSize:13}}
          onKeyDown={e=>e.key==='Enter'&&send()}/>
        <button onClick={send} disabled={loading} style={{background:'var(--accent)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'10px 16px',cursor:'pointer',fontSize:13,display:'flex',alignItems:'center',gap:4}}>
          <Send size={14}/> 发送</button>
      </div>
    </div>
  )
}
