import { useState, useRef, useEffect } from 'react'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { useAppStore, type ChatMsg } from '../stores/app'
import { useToast } from '../components/Toast'
import { Send, Loader2, CheckCircle2, XCircle, RotateCcw, FolderOpen, ArrowUp } from 'lucide-react'
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
  const projectsRef = useRef<any[]>([])
  const toast = useToast(s => s.add)

  useEffect(() => {
    if (activePid !== '_default') { setActiveProject('_default') }
    useAppStore.getState().clearConversation('_default')
    fetchProjects(); fetchTasks()
  }, [])
  useEffect(() => { requestAnimationFrame(() => { bottomRef.current?.scrollIntoView({behavior:'smooth'}) }) }, [msgs, tasks])

  const fetchProjects = async () => {
    try { const d: any = await api.projects(); const list = Array.isArray(d)?d:(d?.projects||[]); setProjects(list); projectsRef.current = list } catch { toast('加载项目失败', 'error') }
  }
  const fetchTasks = async () => {
    try {
      const t = await api.tasks()
      if (Array.isArray(t)) {
        const filtered = activePid !== '_default' ? t.filter((x: any) => x.project_id === activePid) : t
        setTasks(filtered.slice(0, 20).map((x: any) => ({ id: x.id, desc: x.description || '', status: x.status, ts: x.updated_at || Date.now() })))
      }
    } catch { toast('加载任务失败', 'error') }
  }
  const retryFailed = async (tid: string) => { try { await api.retryTask(tid); fetchTasks() } catch { toast('重试失败', 'error') } }

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
    } else if (e.kind === 'observer_answer' && pendingCid.current) {
      try {
        const data = JSON.parse(e.msg || '{}')
        if (data.client_id === pendingCid.current && data.answer) {
          addChatMsg({ role: 'assistant', content: data.answer, ts: Date.now() })
          setLoading(false); pendingCid.current = ''; fetchTasks(); fetchProjects()
          const list = projectsRef.current
          for (const p of list) {
            if (data.answer && data.answer.includes(p.name) && activePid === '_default') { setActiveProject(p.id); break }
          }
        }
      } catch {}
    }
  })

  const send = async () => {
    const q = input.trim(); if (!q || loading) return
    addChatMsg({role:'user',content:q,ts:Date.now()}); setInput(''); setLoading(true)

    if (activePid === '_default') {
      try {
        const r: any = await api.createProject({name: q.slice(0, 30), description: q, template: 'product_dev'})
        if (r?.project?.id) { setActiveProject(r.project.id); await fetchProjects() }
      } catch { toast('创建项目失败', 'error') }
    }

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
  const hasMsgs = msgs.length > 0
  const isEmpty = activePid === '_default' || (!hasMsgs && !loading)

  return (
    <div style={{ display: 'flex', height: '100%', flex: 1 }}>
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', flex: 1, position: 'relative' }}>

      {isEmpty ? (
        <div className="chat-empty">
          <div style={{ fontSize: 24, fontWeight: 600, color: '#ccc', marginBottom: 24 }}>今天想做什么？</div>
          <div style={{ width: '100%', maxWidth: 600 }} className="chat-input-wrap">
            <textarea value={input} onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
              placeholder="发送消息..." rows={1} className="chat-textarea"/>
            <button onClick={send} disabled={!input.trim()} className="chat-send-btn"
              style={{ background: input.trim() ? '#fff' : '#333', color: input.trim() ? '#000' : '#666', cursor: input.trim() ? 'pointer' : 'default' }}>
              <ArrowUp size={14}/>
            </button>
          </div>
          {activePid !== '_default' && info && <div className="fs-11 text-muted" style={{ marginTop: 8 }}>{info.name}</div>}
        </div>
      ) : (
        <>
          <div style={{ flex: 1, overflow: 'auto', padding: '8px 0' }}>
            {msgs.map((m: ChatMsg, i: number) => {
              if (m.role === 'user') {
                return (
                  <div key={i} className="chat-msg-row" style={{ textAlign: 'right', padding: '4px 0' }}>
                    <span className="chat-msg-user">{m.content}</span>
                  </div>
                )
              }
              return <div key={i} className="chat-msg-row chat-msg-assistant">{m.content}</div>
            })}

            {tasks.length > 0 && (
              <div className="chat-msg-row" style={{ marginBottom: 16 }}>
                <div className="flex-center gap-4" style={{ marginBottom: 6 }}>
                  {active > 0 ? <Loader2 size={10} style={{animation:'spin 1s linear infinite'}}/> : <CheckCircle2 size={10} style={{color:'#3fb950'}}/>}
                  <span className="fw-600 fs-11 text-muted">
                    {active > 0 ? `${active} 个执行中` : completed === tasks.length ? '全部完成' : `进度 ${completed}/${tasks.length}`}
                    {failed > 0 && <span style={{ color: '#f85149', marginLeft: 4 }}>{failed} 失败</span>}
                  </span>
                </div>
                {tasks.map((t, i) => {
                  const done = t.status === 'done'; const fail = t.status === 'failed' || t.status === 'cancelled'
                  return (
                    <div key={i} className="flex-center gap-6" style={{ padding: '2px 0' }}>
                      {done ? <CheckCircle2 size={11} color="#3fb950"/> : fail ? <XCircle size={11} color="#f85149"/> : <Loader2 size={11} style={{animation:'spin 1s linear infinite',color:'#999'}}/>}
                      <span className="truncate flex-1 fs-11" style={{ color: done ? '#3fb950' : fail ? '#f85149' : '#999' }}>
                        {t.desc.split('\n')[0].slice(0, 80)}
                      </span>
                      {fail && (
                        <button onClick={e => { e.stopPropagation(); retryFailed(t.id) }}
                          className="btn-icon" style={{ color:'#58a6ff',fontSize:10 }}><RotateCcw size={9}/> 重试</button>
                      )}
                    </div>
                  )
                })}
              </div>
            )}

            {loading && (
              <div className="chat-msg-row flex-center gap-6 text-muted fs-12">
                <Loader2 size={11} style={{animation:'spin 1s linear infinite'}}/>思考中...
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          <div style={{ padding: '0 0 12px' }}>
            <div className="chat-input-wrap-bottom" style={{ maxWidth: 860, margin: '0 auto' }}>
              {info && <span className="fs-11 text-muted" style={{ whiteSpace: 'nowrap' }}>{info.name}</span>}
              <textarea value={input} onChange={e => setInput(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
                placeholder="发送消息..." rows={1} className="chat-textarea"/>
              <button onClick={() => setShowFiles(!showFiles)} className="btn-icon" style={{ color: showFiles?'#58a6ff':'#666' }}>
                <FolderOpen size={15}/>
              </button>
              <button onClick={send} disabled={!input.trim()} className="chat-send-btn"
                style={{ background: input.trim() ? '#fff' : '#333', color: input.trim() ? '#000' : '#666', cursor: input.trim() ? 'pointer' : 'default' }}>
                <ArrowUp size={14}/>
              </button>
            </div>
          </div>
        </>
      )}
      <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
    </div>
    {showFiles && <FilePanel onClose={() => setShowFiles(false)} />}
    </div>
  )
}
