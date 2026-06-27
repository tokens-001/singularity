import { useState, useRef, useEffect } from 'react'
import { api } from '../lib/api'
import { Send, Bot, User } from 'lucide-react'

interface Msg { role: 'user' | 'assistant'; content: string }

let _clientId = ''

export default function ObserverChat() {
  const [msgs, setMsgs] = useState<Msg[]>([{role:'assistant',content:'你好！我是 Observer。可以问我系统状态、任务详情，或者帮你创建任务。'}])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  // Listen for SSE observer_answer events
  useEffect(() => {
    const es = new EventSource('/api/events')
    es.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data)
        if (d.kind === 'observer_answer') {
          const inner = JSON.parse(d.msg)
          const answer = inner.answer || inner.text || ''
          if (answer) {
            setMsgs(prev => [...prev, { role: 'assistant', content: answer }])
            setLoading(false)
          }
        }
      } catch {}
    }
    return () => es.close()
  }, [])

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [msgs])

  const send = async () => {
    if (!input.trim() || loading) return
    const q = input; setInput(''); setMsgs(prev=>[...prev,{role:'user',content:q}]); setLoading(true)
    try {
      const data = await api.observerChat(q)
      _clientId = data?.client_id || ''
    } catch {
      setMsgs(prev=>[...prev,{role:'assistant',content:'Observer 暂时不可用'}])
      setLoading(false)
    }
  }

  return (
    <div style={{display:'flex',flexDirection:'column',height:'calc(100vh - 80px)'}}>
      <div style={{flex:1,overflow:'auto',marginBottom:12}}>
        {msgs.map((m,i)=>(
          <div key={i} style={{display:'flex',gap:8,marginBottom:12,justifyContent:m.role==='user'?'flex-end':'flex-start'}}>
            {m.role==='assistant' && <Bot size={20} color='var(--accent)' style={{marginTop:4,flexShrink:0}}/>}
            <div style={{maxWidth:'70%',background:m.role==='user'?'var(--accent)':'var(--bg-secondary)',color:m.role==='user'?'#fff':'var(--text-primary)',padding:'10px 14px',borderRadius:'var(--radius)',fontSize:13,lineHeight:1.6,whiteSpace:'pre-wrap'}}>
              {m.content}
            </div>
            {m.role==='user' && <User size={20} color='var(--text-muted)' style={{marginTop:4,flexShrink:0}}/>}
          </div>
        ))}
        {loading && <div style={{color:'var(--text-muted)',fontSize:12,padding:8}}>Observer 思考中...</div>}
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
