import { useState, useRef, useEffect } from 'react'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { Send, Bot, User, Play, Square, RefreshCw, Loader2, CheckCircle2, XCircle, Circle, FileText, Wrench } from 'lucide-react'

interface Msg { role: 'user' | 'assistant'; content: string; ts: number }
interface ProgressItem { id: string; desc: string; status: string; ts: number }

export default function Chat() {
  const [msgs, setMsgs] = useState<Msg[]>([{role:'assistant',content:'你好，我是奇点。直接跟我说你想做什么，我来搞定。',ts:Date.now()}])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [loopRunning, setLoopRunning] = useState(false)
  const [tasks, setTasks] = useState<ProgressItem[]>([])
  const [statusText, setStatusText] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => { refreshStatus() }, [])
  useEffect(() => { bottomRef.current?.scrollIntoView({behavior:'smooth'}) }, [msgs, tasks])

  useSSE((e: any) => {
    if (e.kind === 'init') {
      setLoopRunning(e.running)
    } else if (e.kind === 'task') {
      // 实时更新任务进度
      setTasks(prev => {
        const next = [...prev]
        const idx = next.findIndex(t => t.id === e.task_id)
        if (idx >= 0) {
          next[idx] = { ...next[idx], status: e.status || next[idx].status, ts: Date.now() }
        } else if (e.desc) {
          next.push({ id: e.task_id || '', desc: e.desc || e.msg || '', status: e.status || 'running', ts: Date.now() })
        }
        return next.slice(-20)
      })
      if (e.status) refreshStatus()
    } else if (e.kind === 'system') {
      setStatusText(e.msg || '')
      setMsgs(prev => {
        const last = prev[prev.length - 1]
        if (last?.role === 'assistant' && last.content === e.msg) return prev
        return [...prev, { role: 'assistant', content: e.msg || '', ts: Date.now() }]
      })
    }
  })

  const refreshStatus = async () => {
    try {
      const s = await api.status()
      setLoopRunning(!!s.loop_running || !!s.running)
    } catch {}
  }

  const send = async () => {
    const q = input.trim(); if (!q || loading) return
    setMsgs(prev => [...prev, {role:'user',content:q,ts:Date.now()}]); setInput(''); setLoading(true)
    setStatusText('思考中...')
    try {
      const r = await api.observerChat(q)
      const answer = r.answer || r.text || r.result || ''
      if (answer) {
        setMsgs(prev => [...prev, {role:'assistant',content:answer,ts:Date.now()}])
      }
      // Observer 可能创建了任务，刷新任务列表
      setTimeout(async () => {
        try {
          const t = await api.tasks()
          if (Array.isArray(t)) {
            setTasks(t.slice(0, 20).map((x: any) => ({
              id: x.id, desc: x.description || '', status: x.status, ts: x.updated_at || Date.now()
            })))
          }
        } catch {}
      }, 2000)
    } catch {
      setMsgs(prev => [...prev, {role:'assistant',content:'请求失败，请确认后端服务在运行。',ts:Date.now()}])
    }
    setLoading(false)
    setStatusText('')
  }

  const toggleLoop = () => loopRunning ? api.stopLoop().then(refreshStatus) : api.startLoop().then(refreshStatus)

  const completed = tasks.filter(t => t.status === 'done').length
  const failed = tasks.filter(t => t.status === 'failed').length
  const active = tasks.filter(t => !['done','failed','cancelled'].includes(t.status)).length

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', maxWidth: 800, margin: '0 auto', width: '100%' }}>
      {/* 顶栏: 状态 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 10px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', marginBottom: 8, fontSize: 11, fontFamily: 'var(--font-mono)' }}>
        <span style={{ color: loopRunning ? 'var(--accent-green)' : 'var(--text-muted)', fontSize: 10 }}>
          {loopRunning ? '◆ 运行中' : '◇ 待机'}
        </span>
        {active > 0 && <span style={{ color: 'var(--accent)' }}>{active} 进行中</span>}
        {completed > 0 && <span style={{ color: 'var(--accent-green)' }}>{completed} 完成</span>}
        {failed > 0 && <span style={{ color: 'var(--accent-red)' }}>{failed} 失败</span>}
        <span style={{ flex: 1 }} />
        <button onClick={refreshStatus} style={{background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2}}><RefreshCw size={12}/></button>
        <button onClick={toggleLoop} style={{background:'none',border:'none',color:loopRunning?'var(--accent-red)':'var(--accent-green)',cursor:'pointer',padding:2}}>
          {loopRunning ? <Square size={14}/> : <Play size={14}/>}
        </button>
      </div>

      {/* 主体: 对话 + 进度 */}
      <div style={{ flex: 1, overflow: 'auto' }}>
        {msgs.map((m, i) => (
          <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 12, alignItems: 'flex-start' }}>
            <span style={{ marginTop: 2, color: m.role === 'user' ? 'var(--accent)' : 'var(--accent-green)', flexShrink: 0 }}>
              {m.role === 'user' ? <User size={14}/> : <Bot size={14}/>}
            </span>
            <div style={{ flex: 1, fontSize: 13, color: 'var(--text-primary)', whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>
              {m.content}
            </div>
          </div>
        ))}

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
          <div style={{ marginLeft: 22, display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-muted)', fontSize: 12 }}>
            <Loader2 size={12} style={{animation:'spin 1s linear infinite'}}/>
            {statusText || '思考中...'}
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
