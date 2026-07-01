import { useState, useRef, useEffect } from 'react'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { useAppStore, type ChatMsg } from '../stores/app'
import { Send, Bot, User, Loader2, CheckCircle2, XCircle } from 'lucide-react'

interface ProgressItem { id: string; desc: string; status: string; ts: number }

export default function Chat() {
  const msgs = useAppStore(s => s.chatMsgs)
  const addChatMsg = useAppStore(s => s.addChatMsg)
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [tasks, setTasks] = useState<ProgressItem[]>([])
  const [statusText, setStatusText] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const pendingCid = useRef<string>('')

  useEffect(() => {
    if (msgs.length === 0) {
      addChatMsg({role:'assistant',content:'你好，我是奇点。直接跟我说你想做什么，我来搞定。',ts:Date.now()})
    }
    fetchTasks()
  }, [])
  useEffect(() => { bottomRef.current?.scrollIntoView({behavior:'smooth'}) }, [msgs, tasks])

  const fetchTasks = async () => {
    try {
      const t = await api.tasks()
      if (Array.isArray(t)) {
        setTasks(t.slice(0, 20).map((x: any) => ({
          id: x.id, desc: x.description || '', status: x.status, ts: x.updated_at || Date.now()
        })))
      }
    } catch {}
  }

  useSSE((e: any) => {
    if (e.kind === 'task') {
      // 解析 task 事件: msg 字段可能是 JSON 字符串或直接在顶层
      let td: any = e
      if (e.msg && typeof e.msg === 'string') {
        try { const p = JSON.parse(e.msg); if (p.task_id) td = p } catch {}
      }
      const tid = td.task_id || ''
      const status = td.status || 'running'
      const desc = td.desc || e.msg || ''
      setTasks(prev => {
        const next = [...prev]
        const idx = next.findIndex(t => t.id === tid)
        if (idx >= 0) {
          next[idx] = { ...next[idx], status, ts: Date.now() }
        } else if (desc) {
          next.push({ id: tid, desc, status, ts: Date.now() })
        }
        return next.slice(-20)
      })
    } else if (e.kind === 'system') {
      setStatusText(e.msg || '')
      const last = msgs[msgs.length - 1]
      if (last?.role !== 'assistant' || last.content !== e.msg) {
        addChatMsg({ role: 'assistant', content: e.msg || '', ts: Date.now() })
      }
    } else if (e.kind === 'observer_answer') {
      // Observer 异步回复
      try {
        const data = JSON.parse(e.msg || '{}')
        if (data.client_id === pendingCid.current && data.answer) {
          addChatMsg({ role: 'assistant', content: data.answer, ts: Date.now() })
          setLoading(false)
          setStatusText('')
          pendingCid.current = ''
          fetchTasks()
        }
      } catch {}
    }
  })

  const send = async () => {
    const q = input.trim(); if (!q || loading) return
    addChatMsg({role:'user',content:q,ts:Date.now()}); setInput(''); setLoading(true)
    setStatusText('思考中...')
    try {
      const r = await api.observerChat(q)
      if (r.client_id) {
        pendingCid.current = r.client_id
        setStatusText('Observer 思考中...')
      } else if (r.answer) {
        addChatMsg({role:'assistant',content:r.answer,ts:Date.now()})
        setLoading(false)
        setStatusText('')
      }
    } catch {
      addChatMsg({role:'assistant',content:'请求失败，请确认后端服务在运行。',ts:Date.now()})
      setLoading(false)
      setStatusText('')
    }
  }

  const completed = tasks.filter(t => t.status === 'done').length
  const failed = tasks.filter(t => t.status === 'failed').length
  const active = tasks.filter(t => !['done','failed','cancelled'].includes(t.status)).length

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', maxWidth: 800, margin: '0 auto', width: '100%' }}>
      {/* 顶栏: 有任务时显示进度 */}
      {(active > 0 || completed > 0 || failed > 0) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 10px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', marginBottom: 8, fontSize: 11, fontFamily: 'var(--font-mono)' }}>
          {active > 0 && <span style={{ color: 'var(--accent)' }}>{active} 进行中</span>}
          {completed > 0 && <span style={{ color: 'var(--accent-green)' }}>{completed} 完成</span>}
          {failed > 0 && <span style={{ color: 'var(--accent-red)' }}>{failed} 失败</span>}
          <span style={{ flex: 1 }} />
        </div>
      )}

      {/* 主体: 对话 + 进度 */}
      <div style={{ flex: 1, overflow: 'auto' }}>
        {msgs.map((m, i) => {
          const isUser = m.role === 'user'
          return (
          <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 12, alignItems: 'flex-start', flexDirection: isUser ? 'row-reverse' : 'row' }}>
            <span style={{ marginTop: 2, color: isUser ? 'var(--accent)' : 'var(--accent-green)', flexShrink: 0 }}>
              {isUser ? <User size={14}/> : <Bot size={14}/>}
            </span>
            <div style={{
              maxWidth: '75%', fontSize: 13, whiteSpace: 'pre-wrap', lineHeight: 1.6,
              padding: '8px 12px', borderRadius: 10,
              background: isUser ? 'var(--accent)' : 'var(--bg-secondary)',
              color: isUser ? '#fff' : 'var(--text-primary)',
            }}>
              {m.content}
            </div>
          </div>
        )})}

        {/* 实时进度卡片 */}
        {tasks.length > 0 && (
          <div style={{ marginLeft: 22, marginBottom: 12 }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-secondary)', marginBottom: 4 }}>
              {active > 0 ? <Loader2 size={10} style={{display:'inline',marginRight:4,animation:'spin 1s linear infinite'}} /> : <CheckCircle2 size={10} style={{display:'inline',marginRight:4,color:'var(--accent-green)'}}/>}
              {active > 0 ? '正在执行...' : completed === tasks.length ? '全部完成' : `任务进度 (${completed}/${tasks.length})`}
            </div>
            {tasks.map((t, i) => {
              const done = t.status === 'done'
              const fail = t.status === 'failed' || t.status === 'cancelled'
              const running = !done && !fail
              return (
                <div key={i} style={{
                  display: 'flex', alignItems: 'center', gap: 6, padding: '3px 8px',
                  fontSize: 11, color: done ? 'var(--accent-green)' : fail ? 'var(--accent-red)' : 'var(--text-secondary)',
                  opacity: done || fail ? 0.8 : 1,
                }}>
                  {done ? <CheckCircle2 size={12}/> : fail ? <XCircle size={12}/> : <Loader2 size={12} style={{animation:'spin 1s linear infinite'}}/>}
                  <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {t.desc.length > 80 ? t.desc.slice(0,80) + '...' : t.desc || (running ? '执行中...' : '') }
                  </span>
                  {running && <span style={{ fontSize: 9, color: 'var(--text-muted)' }}>···</span>}
                </div>
              )
            })}
          </div>
        )}

        {/* Observer 思考中 */}
        {loading && (
          <div style={{ display: 'flex', gap: 8, marginBottom: 12, alignItems: 'center' }}>
            <Bot size={14} style={{ color: 'var(--accent-green)', flexShrink: 0 }}/>
            <div style={{ padding: '8px 12px', borderRadius: 10, background: 'var(--bg-secondary)', fontSize: 13, color: 'var(--text-muted)' }}>
              <Loader2 size={12} style={{animation:'spin 1s linear infinite', display:'inline', marginRight:6}}/>
              {statusText || '思考中...'}
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* 底部输入 */}
      <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
        <input value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
          placeholder="跟我说你想做什么..."
          style={{ flex: 1, background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: '10px 14px', color: 'var(--text-primary)', fontSize: 13 }} />
        <button onClick={send} disabled={loading}
          style={{ background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: 'var(--radius)', padding: '10px 16px', cursor: 'pointer', fontWeight: 600, fontSize: 13 }}>
          发送
        </button>
      </div>

      <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}
