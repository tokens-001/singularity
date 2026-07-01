import { useState, useRef, useEffect } from 'react'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { useAppStore, type ChatMsg } from '../stores/app'
import { Send, Loader2, CheckCircle2, XCircle, PanelRightOpen, RotateCcw } from 'lucide-react'
import FilePanel from '../components/FilePanel'

interface ProgressItem { id: string; desc: string; status: string; ts: number }

export default function Chat() {
  const conversations = useAppStore(s => s.conversations)
  const activePid = useAppStore(s => s.activeProjectId)
  const setActiveProject = useAppStore(s => s.setActiveProject)
  const addChatMsg = useAppStore(s => s.addChatMsg)
  const msgs = conversations[activePid] || []

  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [showFiles, setShowFiles] = useState(false)
  const [tasks, setTasks] = useState<ProgressItem[]>([])
  const [projects, setProjects] = useState<any[]>([])
  const bottomRef = useRef<HTMLDivElement>(null)
  const pendingCid = useRef<string>('')
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    fetchProjects(); fetchTasks()
    if (msgs.length === 0 && activePid === '_default') {
      addChatMsg({role:'assistant',content:'你好，我是奇点。直接跟我说你想做什么，我来搞定。',ts:Date.now()})
    }
  }, [activePid])
  useEffect(() => { bottomRef.current?.scrollIntoView({behavior:'smooth'}) }, [msgs, tasks])

  const fetchProjects = async () => {
    try { const d: any = await api.projects(); setProjects(Array.isArray(d)?d:(d?.projects||[])) } catch {}
  }
  const retryFailed = async (tid: string) => {
    try { await api.retryTask(tid); fetchTasks() } catch {}
  }
  const fetchTasks = async () => {
    try {
      const t = await api.tasks()
      if (Array.isArray(t)) setTasks(t.slice(0, 20).map((x: any) => ({ id: x.id, desc: x.description || '', status: x.status, ts: x.updated_at || Date.now() })))
    } catch {}
  }

  useSSE((e: any) => {
    if (e.kind === 'task') {
      let td: any = e
      if (e.msg && typeof e.msg === 'string') { try { const p = JSON.parse(e.msg); if (p.task_id) td = p } catch {} }
      const tid = td.task_id || ''; const status = td.status || 'running'; const desc = td.desc || e.msg || ''
      if (td.project_id && activePid === '_default') setActiveProject(td.project_id)
      setTasks(prev => {
        const next = [...prev]; const idx = next.findIndex(t => t.id === tid)
        if (idx >= 0) next[idx] = { ...next[idx], status, ts: Date.now() }
        else if (desc) next.push({ id: tid, desc, status, ts: Date.now() })
        return next.slice(-20)
      })
    } else if (e.kind === 'system') {
      if (e.project_id && activePid === '_default') setActiveProject(e.project_id)
      const last = msgs[msgs.length - 1]
      if (last?.role !== 'assistant' || last.content !== e.msg) addChatMsg({ role: 'assistant', content: e.msg || '', ts: Date.now() })
    } else if (e.kind === 'observer_answer') {
      try {
        const data = JSON.parse(e.msg || '{}')
        if (data.client_id === pendingCid.current && data.answer) {
          addChatMsg({ role: 'assistant', content: data.answer, ts: Date.now() })
          setLoading(false); pendingCid.current = ''; fetchTasks(); fetchProjects()
          setTimeout(() => {
            for (const p of projects) {
              if (data.answer && data.answer.includes(p.name) && activePid === '_default') { setActiveProject(p.id); break }
            }
          }, 500)
        }
      } catch {}
    }
  })

  const send = async () => {
    const q = input.trim(); if (!q || loading) return
    addChatMsg({role:'user',content:q,ts:Date.now()}); setInput(''); setLoading(true)
    try {
      const r = await api.observerChat(q)
      if (r.client_id) { pendingCid.current = r.client_id }
      else if (r.answer) { addChatMsg({role:'assistant',content:r.answer,ts:Date.now()}); setLoading(false) }
    } catch { addChatMsg({role:'assistant',content:'请求失败，请确认后端服务在运行。',ts:Date.now()}); setLoading(false) }
  }

  const completed = tasks.filter(t => t.status === 'done').length
  const failed = tasks.filter(t => t.status === 'failed').length
  const active = tasks.filter(t => !['done','failed','cancelled'].includes(t.status)).length

  const info = activePid !== '_default' ? projects.find(p => p.id === activePid) : null

  return (
    <div style={{ display: 'flex', height: '100%', flex: 1 }}>
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', flex: 1, maxWidth: 900, margin: '0 auto', width: '100%' }}>

      {/* 项目信息条 */}
      {info && (
        <div style={{ padding: '4px 0 8px', fontSize: 12, color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: 8, borderBottom: '1px solid var(--border)', marginBottom: 4 }}>
          <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{info.name}</span>
          <span>·</span><span>{info.phase}</span>
          <span>·</span><span>{info.task_count||0} 任务</span>
          <span style={{ flex:1 }}/>
          <button onClick={() => setShowFiles(!showFiles)} style={{ background:'none',border:'none',color: showFiles?'var(--accent)':'var(--text-muted)',cursor:'pointer',padding:2 }}>
            <PanelRightOpen size={14}/>
          </button>
        </div>
      )}

      {/* 消息列表 */}
      <div style={{ flex: 1, overflow: 'auto', paddingBottom: 8 }}>
        {msgs.map((m: ChatMsg, i: number) => {
          const isUser = m.role === 'user'
          return (
          <div key={i} style={{ marginBottom: isUser ? 6 : 16 }}>
            {isUser ? (
              <div style={{ padding: '4px 0', fontSize: 13, color: 'var(--text-primary)' }}>
                {m.content}
              </div>
            ) : (
              <div style={{ fontSize: 13, color: 'var(--text-primary)', lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>
                {m.content}
              </div>
            )}
          </div>
        )})}

        {/* 任务进度 */}
        {tasks.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', marginBottom: 6, display: 'flex', alignItems: 'center', gap: 4 }}>
              {active > 0 ? <Loader2 size={10} style={{animation:'spin 1s linear infinite'}}/> : <CheckCircle2 size={10} style={{color:'var(--accent-green)'}}/>}
              {active > 0 ? `${active} 个执行中` : completed === tasks.length ? '全部完成' : `进度 ${completed}/${tasks.length}`}
              {failed > 0 && <span style={{ color: 'var(--accent-red)', marginLeft: 4 }}>{failed} 失败</span>}
            </div>
            {tasks.map((t, i) => {
              const done = t.status === 'done'; const fail = t.status === 'failed' || t.status === 'cancelled'
              return (
                <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0', fontSize: 11, color: done ? 'var(--accent-green)' : fail ? 'var(--accent-red)' : 'var(--text-secondary)' }}>
                  {done ? <CheckCircle2 size={11}/> : fail ? <XCircle size={11}/> : <Loader2 size={11} style={{animation:'spin 1s linear infinite'}}/>}
                  <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {t.desc.length > 100 ? t.desc.slice(0,100)+'...' : t.desc}
                  </span>
                  {fail && (
                    <button onClick={e => { e.stopPropagation(); retryFailed(t.id) }}
                      style={{ background:'none',border:'none',color:'var(--accent)',cursor:'pointer',padding:'0 4px',fontSize:10 }}>
                      <RotateCcw size={9}/> 重试
                    </button>
                  )}
                </div>
              )
            })}
          </div>
        )}

        {/* 思考中 */}
        {loading && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-muted)', fontSize: 12, marginBottom: 16 }}>
            <Loader2 size={11} style={{animation:'spin 1s linear infinite'}}/>
            思考中...
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* 底部输入 */}
      <div style={{ padding: '8px 0', borderTop: '1px solid var(--border)' }}>
        <div style={{ display: 'flex', gap: 8, background: 'var(--bg-secondary)', borderRadius: 8, padding: '6px 8px', border: '1px solid var(--border)' }}>
          <textarea ref={inputRef} value={input} onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
            placeholder="发送消息..."
            rows={1}
            style={{ flex: 1, background: 'transparent', border: 'none', outline: 'none', color: 'var(--text-primary)', fontSize: 13, fontFamily: 'inherit', resize: 'none', padding: '4px 0', lineHeight: '20px' }}/>
          <button onClick={send} disabled={loading || !input.trim()}
            style={{ background: input.trim() ? 'var(--accent)' : 'var(--bg-tertiary)', color: input.trim() ? '#fff' : 'var(--text-muted)', border: 'none', borderRadius: 6, padding: '4px 10px', cursor: input.trim() ? 'pointer' : 'default', display: 'flex', alignItems: 'center', transition: '0.15s' }}>
            <Send size={14}/>
          </button>
        </div>
      </div>
      <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
    </div>
    {showFiles && <FilePanel onClose={() => setShowFiles(false)} />}
    </div>
  )
}
