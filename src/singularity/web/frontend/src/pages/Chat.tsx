import { useState, useRef, useEffect, useMemo } from 'react'
import { Bubble, Sender } from '@ant-design/x'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { useAppStore, type ChatMsg } from '../stores/app'
import { useToast } from '../lib/toast'
import { Loader2, CheckCircle2, FolderOpen } from 'lucide-react'
import FilePanel from '../components/FilePanel'
import { MessageBubble } from '../components/chat/MessageBubble'
import { TaskCard, type ProgressItem, type ToolLog } from '../components/chat/TaskCard'
import { GatePanel } from '../components/chat/GatePanel'
import { ChatOptions, type ExecMode } from '../components/chat/ChatOptions'

const PHASE_NAMES: Record<string, string> = { template: '待开始', researching: '调研中', planning: '架构设计中', executing: '实现中', integrating: '集成合并中', reviewing: '审查中', delivering: '交付中', done: '已完成' }

export default function Chat() {
  const conversations = useAppStore(s => s.conversations)
  const activePid = useAppStore(s => s.activeProjectId)
  const setActiveProject = useAppStore(s => s.setActiveProject)
  const addChatMsg = useAppStore(s => s.addChatMsg)
  const msgs = conversations[activePid] || []

  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [showFiles, setShowFiles] = useState(false)
  const [execMode, setExecMode] = useState<ExecMode>('auto_edit')
  const [tasks, setTasks] = useState<ProgressItem[]>([])
  const [projects, setProjects] = useState<any[]>([])
  const [status, setStatus] = useState<any>(null)
  const [acceptance, setAcceptance] = useState<any>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const stickBottom = useRef(true)   // 用户手动往上翻时不再自动追底
  const pendingCid = useRef<string>('')
  const projectsRef = useRef<any[]>([])
  const traceCache = useRef<Map<string, { files: string[]; verdict: string }>>(new Map())
  const fetchSeq = useRef(0)   // fetchTasks 的请求序号，用于丢弃过期响应
  const toast = useToast()

  useEffect(() => {
    // 这里原来每次挂载都 clearConversation('_default') —— 而 Chat 是路由页，
    // 离开对话页再回来就把通用会话（没绑项目时的聊天）整段删了，
    // 且 store 持久化到 localStorage，刷新也找不回。没有任何"必须重置"的理由。
    fetchProjects(); fetchStatus()
  }, [])

  // 切项目必须重拉任务，否则 B 项目里还挂着 A 的任务卡片
  useEffect(() => { setTasks([]); fetchTasks() }, [activePid])

  useEffect(() => {
    if (!stickBottom.current) return
    requestAnimationFrame(() => { const el = scrollRef.current; if (el) el.scrollTop = el.scrollHeight })
  }, [msgs, tasks])

  const onScroll = () => {
    const el = scrollRef.current
    if (el) stickBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  // GATE3 的验收依据（QA 报告 / 需求符合性）走单独接口拉 —— 否则这道门只显示几周的旧文档。
  // 依赖用 phase 字符串而不是 projects 数组：fetchProjects 每次 SSE 事件都会换新数组，
  // 而 /traceability 内部可能调 LLM，不能跟着刷新反复打。
  const activePhase = projects.find(p => p.id === activePid)?.phase || ''
  useEffect(() => {
    if (activePhase !== 'gate3') { setAcceptance(null); return }
    let cancelled = false
    api.traceability(activePid)
      .then((d: any) => { if (!cancelled) setAcceptance(d) })
      .catch(() => { if (!cancelled) setAcceptance(null) })
    return () => { cancelled = true }
  }, [activePid, activePhase])

  const fetchStatus = async () => {
    try { setStatus(await api.status()) } catch { /* 状态轮询失败不打扰用户 */ }
  }
  const pickRoot = async () => {
    try {
      const d: any = await api.fsPick()
      if (d?.path) { await api.setProjectsRoot(d.path); fetchStatus(); toast('项目根目录已设置', 'success') }
    } catch (e) { toast(String(e), 'error') }
  }
  const fetchProjects = async () => {
    try { const d: any = await api.projects(); const list = Array.isArray(d)?d:(d?.projects||[]); setProjects(list); projectsRef.current = list } catch { toast('加载项目失败', 'error') }
  }
  const fetchTasks = async () => {
    // 请求序号：只认最后一次发出的请求的结果。
    // 原来 A/B 两个项目快速来回切时，先发的那次响应后到会**覆盖**新项目的列表
    // （闭包里的 activePid 只决定过滤条件，拦不住"先发后到"）。
    const seq = ++fetchSeq.current
    try {
      const t = await api.tasks()
      if (seq !== fetchSeq.current) return   // 已有更新的请求发出 → 丢弃这次结果
      if (Array.isArray(t)) {
        const filtered = activePid !== '_default' ? t.filter((x: any) => x.project_id === activePid) : t
        const list = filtered.slice(0, 20)
        setTasks(list.map((x: any) => {
          const c = traceCache.current.get(x.id)
          return { id: x.id, desc: x.description || '', status: x.status, ts: x.updated_at || Date.now(), route_type: x.route_type || '', duration: x.duration_sec, error: x.error || '', files: c?.files, verdict: c?.verdict }
        }))
        // 已完成任务懒拉产物(changed_files)+校验结论，缓存防重复请求
        list.forEach((x: any) => {
          if (x.status === 'done' && !traceCache.current.has(x.id)) {
            traceCache.current.set(x.id, { files: [], verdict: '' })
            api.taskTrace(x.id, 'validation').then((d: any) => {
              const files = d?.changed_files || []
              const verdict = d?.verdict || ''
              traceCache.current.set(x.id, { files, verdict })
              setTasks(prev => prev.map(t => t.id === x.id ? { ...t, files, verdict } : t))
            }).catch(() => {
              // 失败要把占位删掉。原来留着空占位，而上面 `!traceCache.current.has(id)`
              // 是唯一的重试闸门 → 一次瞬时失败 = 这个任务的产物**终生不再拉取**。
              traceCache.current.delete(x.id)
            })
          }
        })
      }
    } catch { toast('加载任务失败', 'error') }
  }
  const retryFailed = async (tid: string) => { try { await api.retryTask(tid); fetchTasks() } catch { toast('重试失败', 'error') } }
  // 带上 activePid：项目产出的文件在成品仓库里，不带项目 id 会拿奇点自己的根去找 → 必 404
  const revealFile = (f: string) => { api.revealFile(f, activePid).catch((e: any) => toast(`定位失败：${e?.message || e}`, 'error')) }

  useSSE((e: any) => {
    if (e.kind === 'task') {
      let td: any = e
      if (e.msg && typeof e.msg === 'string') { try { const p = JSON.parse(e.msg); if (p.task_id) td = p } catch {} }
      const tid = td.task_id || ''; const status = td.status || 'running'; const desc = td.desc || e.msg || ''
      if (td.project_id && activePid === '_default') setActiveProject(td.project_id)
      // 只收当前项目的事件：否则别的项目一跑任务，当前对话里就冒出它的卡片
      if (activePid !== '_default' && td.project_id !== activePid) return
      setTasks(prev => {
        const next = [...prev]; const idx = next.findIndex(t => t.id === tid)
        if (idx >= 0) next[idx] = { ...next[idx], status, ts: Date.now() }
        else if (desc) next.push({ id: tid, desc, status, ts: Date.now() })
        return next.slice(-20)
      })
    } else if (e.kind === 'tool:start' || e.kind === 'tool:done' || e.kind === 'gen') {
      const tid = e.task_id || ''
      if (tid) {
        const log: ToolLog = { tool: e.tool || '', kind: e.kind, msg: e.msg || '', ts: e.ts || Date.now() }
        setTasks(prev => prev.map(t => t.id === tid ? { ...t, logs: [...(t.logs || []), log].slice(-30) } : t))
      }
    } else if (e.kind === 'system') {
      if (e.project_id && activePid === '_default') setActiveProject(e.project_id)
      const last = msgs[msgs.length - 1]
      if (last?.role !== 'assistant' || last.content !== e.msg) addChatMsg({ role: 'assistant', content: e.msg || '', ts: Date.now() })
      // 刷新项目状态 (phase变化)
      if (e.project_id) fetchProjects()
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

  const send = async (text?: string) => {
    const q = (text ?? input).trim(); if (!q || loading) return
    setInput(''); setLoading(true)
    stickBottom.current = true

    // 先把项目定下来、**再**插消息。
    // 原来顺序反了：先 addChatMsg 落到 '_default'，随后 setActiveProject 切到新项目，
    // 渲染读的是 conversations[新id] → 用户自己那条消息当场从屏幕上消失。
    // （addChatMsg 取 store 的 activeProjectId，setActiveProject 是同步写，顺序能保证落对会话。）
    let pid = activePid
    if (pid === '_default') {
      try {
        const r: any = await api.createProject({name: q.slice(0, 30), description: q, template: 'product_dev'})
        if (r?.project?.id) { pid = r.project.id; setActiveProject(pid); await fetchProjects() }
      } catch { toast('创建项目失败', 'error') }
    }
    addChatMsg({role:'user',content:q,ts:Date.now()})

    try {
      const r = await api.observerChat(q, execMode, pid !== '_default' ? pid : '')
      if (r.client_id) { pendingCid.current = r.client_id }
      else if (r.answer) { addChatMsg({role:'assistant',content:r.answer,ts:Date.now()}); setLoading(false) }
    } catch { addChatMsg({role:'assistant',content:'请求失败，请确认后端服务在运行。',ts:Date.now()}); setLoading(false) }
  }

  // 停止等待：后端仍在跑，只是不再接收这条回答（清掉 client_id 后到达的 observer_answer 会被忽略）
  const cancelWait = () => { setLoading(false); pendingCid.current = '' }

  const gateConfirm = async (decision: 'approved' | 'rejected') => {
    const info = projects.find(p => p.id === activePid)
    if (!info) return
    try { await api.gateConfirm(info.id, info.phase, decision); fetchProjects(); fetchTasks() }
    catch { toast('操作失败', 'error') }
  }

  const { completed, failed, active } = useMemo(() => {
    let c = 0, f = 0, a = 0
    for (const t of tasks) {
      if (t.status === 'done') c++
      else if (t.status === 'failed' || t.status === 'cancelled') f++
      else a++
    }
    return { completed: c, failed: f, active: a }
  }, [tasks])

  const info = activePid !== '_default' ? projects.find(p => p.id === activePid) : null
  const gatePhase = info?.phase || ''
  const isGate = gatePhase.startsWith('gate')
  const gateNum = isGate ? gatePhase.replace('gate','') : ''
  const isEmpty = activePid === '_default'

  return (
    <div style={{ display: 'flex', height: '100%', flex: 1 }}>
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', flex: 1, position: 'relative' }}>

      {isEmpty ? (
        <div className="chat-empty">
          <div style={{ width: '100%', maxWidth: 860 }}>
            <Sender value={input} onChange={(v) => setInput(v)} onSubmit={(v) => send(v)}
              loading={loading} onCancel={cancelWait} placeholder="描述你的项目或任务，回车开始…"
              autoSize={{ minRows: 1, maxRows: 6 }}/>
          </div>
          <div style={{ width: '100%', maxWidth: 860, display: 'flex', alignItems: 'center', gap: 10, marginTop: 12, flexWrap: 'wrap', justifyContent: 'space-between' }}>
            <ChatOptions workdir={status?.workdir} execMode={execMode} onModeChange={setExecMode} onPickRoot={pickRoot} />
          </div>
        </div>
      ) : (
        <>
          <div ref={scrollRef} onScroll={onScroll} style={{ flex: 1, overflow: 'auto', padding: '8px 0' }}>
            {msgs.map((m: ChatMsg, i: number) => <MessageBubble key={i} m={m} />)}

            {tasks.length > 0 && (
              <div className="chat-msg-row" style={{ marginBottom: 16 }}>
                <div className="flex-center gap-4" style={{ marginBottom: 6 }}>
                  {active > 0 ? <Loader2 size={10} style={{animation:'spin 1s linear infinite'}}/> : <CheckCircle2 size={10} style={{color:'#16a34a'}}/>}
                  <span className="fw-600 fs-11 text-muted">
                    {active > 0 ? `${active} 个执行中` : completed === tasks.length ? '全部完成' : `进度 ${completed}/${tasks.length}`}
                    {failed > 0 && <span style={{ color: '#dc2626', marginLeft: 4 }}>{failed} 失败</span>}
                  </span>
                  {info && <span className="fs-11" style={{ marginLeft: 'auto', color: '#2563eb', fontWeight: 600 }}>{PHASE_NAMES[gatePhase] || gatePhase}</span>}
                </div>
                <div style={{ height: 6, background: '#e5e2d8', borderRadius: 999, overflow: 'hidden', marginBottom: 8 }}>
                  <div style={{ height: '100%', width: `${tasks.length ? Math.round((completed / tasks.length) * 100) : 0}%`, background: '#16a34a', borderRadius: 999, transition: 'width 0.3s' }} />
                </div>
                {tasks.map((t) => <TaskCard key={t.id} t={t} onRetry={retryFailed} onReveal={revealFile} />)}
              </div>
            )}

            {isGate && info && (
              <GatePanel info={info} gateNum={gateNum} gatePhase={gatePhase}
                acceptance={acceptance} onGate={gateConfirm} />
            )}

            {loading && (
              <div className="chat-msg-row" style={{ padding: '4px 0' }}>
                <Bubble placement="start" variant="borderless" content="" loading
                  loadingRender={() => (
                    <span className="flex-center gap-6 text-muted fs-12">
                      <Loader2 size={11} style={{animation:'spin 1s linear infinite'}}/>思考中...
                    </span>
                  )}/>
              </div>
            )}
          </div>

          <div style={{ padding: '0 0 12px' }}>
            <div style={{ maxWidth: 860, margin: '0 auto' }}>
              {info && <div className="fs-11 text-muted" style={{ marginBottom: 4 }}>
                {info.name} <span style={{color: isGate?'#16a34a':gatePhase==='done'?'#16a34a':'#9a9993'}}>· {PHASE_NAMES[gatePhase] || gatePhase}</span>
              </div>}
              <Sender value={input} onChange={(v) => setInput(v)} onSubmit={(v) => send(v)}
                loading={loading} onCancel={cancelWait} placeholder="发送消息..." autoSize={{ minRows: 1, maxRows: 6 }}
                prefix={<button onClick={() => setShowFiles(!showFiles)} className="btn-icon" aria-label="文件面板" style={{ color: showFiles?'#2563eb':'#9a9993' }}><FolderOpen size={15}/></button>}/>
            </div>
            <div style={{ maxWidth: 860, margin: '6px auto 0', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', justifyContent: 'space-between' }}>
              <ChatOptions workdir={status?.workdir} execMode={execMode} onModeChange={setExecMode} onPickRoot={pickRoot} />
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
