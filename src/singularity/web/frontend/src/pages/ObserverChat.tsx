import { useState, useRef } from 'react'
import { api } from '../lib/api'
import { Send, Bot, User } from 'lucide-react'

interface Msg { role: 'user' | 'assistant'; content: string }

export default function ObserverChat() {
  const [msgs, setMsgs] = useState<Msg[]>([{role:'assistant',content:'你好！我是 Observer。可以问我系统状态、任务详情，或者帮你创建任务。'}])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  const send = async () => {
    if (!input.trim() || loading) return
    const q = input; setInput(''); setMsgs(prev=>[...prev,{role:'user',content:q}]); setLoading(true)
    try {
      const data = await api.observerChat(q)
      const answer = data?.answer || data?.text || JSON.stringify(data)
      setMsgs(prev=>[...prev,{role:'assistant',content:answer}])
    } catch {
      setMsgs(prev=>[...prev,{role:'assistant',content:'调用失败，请检查 Observer 是否运行中'}])
    } finally { setLoading(false) }
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
        {loading && <div style={{color:'var(--text-muted)',fontSize:12}}>思考中...</div>}
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
