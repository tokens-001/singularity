import { useState, useRef, useEffect, useMemo } from 'react'
import { Bubble, Sender } from '@ant-design/x'
import { api } from '../lib/api'
import { useSSE, useSSEConnected } from '../lib/useSSE'
import { useAppStore, type ChatMsg } from '../stores/app'
import { useToast, errText } from '../lib/toast'
import { Loader2, CheckCircle2, AlertCircle, FolderOpen } from 'lucide-react'
import FilePanel from '../components/FilePanel'
import { MessageBubble } from '../components/chat/MessageBubble'
import { TaskCard, taskStateKind, type ProgressItem, type ToolLog } from '../components/chat/TaskCard'
import { GateBar, ProjectMaterials } from '../components/chat/GatePanel'
import { ChatOptions, type ExecMode } from '../components/chat/ChatOptions'

const PHASE_NAMES: Record<string, string> = { template: '待开始', researching: '调研中', planning: '架构设计中', executing: '实现中', integrating: '集成合并中', reviewing: '审查中', delivering: '交付中', done: '已完成' }

/** `12345` → `12.3k`。用量那行只求"一眼看出量级"，不求精确。 */
function fmtTokens(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`
  return String(n)
}

export default function Chat() {
  const conversations = useAppStore(s => s.conversations)
  const activePid = useAppStore(s => s.activeProjectId)
  const setActiveProject = useAppStore(s => s.setActiveProject)
  const addChatMsg = useAppStore(s => s.addChatMsg)
  const msgs = conversations[activePid] || []

  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [showFiles, setShowFiles] = useState(false)
  // 材料侧滑面板。**默认收起** —— 正文留给观察者说话（2026-09-17）。
  const [showMaterials, setShowMaterials] = useState(false)
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
    try { const d: any = await api.projects(); const list = Array.isArray(d)?d:(d?.projects||[]); setProjects(list); projectsRef.current = list } catch (e) { toast(errText(e, '加载项目失败'), 'error') }
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
          return { id: x.id, desc: x.description || '', status: x.status, ts: x.updated_at || Date.now(), route_type: x.route_type || '', duration: x.duration_sec, error: x.error || '', salvage_ref: x.salvage_ref || '', files: c?.files, verdict: c?.verdict }
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
    } catch (e) { toast(errText(e, '加载任务失败'), 'error') }
  }
  const retryFailed = async (tid: string) => { try { await api.retryTask(tid); fetchTasks() } catch (e) { toast(errText(e, '重试失败'), 'error') } }
  // 带上 activePid：项目产出的文件在成品仓库里，不带项目 id 会拿奇点自己的根去找 → 必 404
  const revealFile = (f: string) => { api.revealFile(f, activePid).catch((e: any) => toast(errText(e, '定位失败'), 'error')) }

  // ⚠️ **SSE 断了要有兜底，而且要看得见**（2026-09-14，外派④扫前端抓出）：
  // Chat 页的任务卡片**全靠 SSE 事件**驱动（`fetchTasks` 只在挂载/切换时跑），
  // 所以连接一死，卡片就**冻在最后一帧**、界面上**一点提示都没有** ——
  // 用户只会以为"任务卡住了"，而实际上任务早跑完了。
  // `Tasks.tsx` / `AppLayout.tsx` 早就有 `useSSEConnected` 开的轮询兜底，**这里漏了**。
  const sseAlive = useSSEConnected()
  const fetchTasksRef = useRef(fetchTasks)
  fetchTasksRef.current = fetchTasks          // 用 ref 稳住：`fetchTasks` 每次渲染都是新函数
  useEffect(() => {
    if (sseAlive) return                      // SSE 活着 → 事件驱动，不轮询
    const t = setInterval(() => { fetchTasksRef.current() }, 10000)
    return () => clearInterval(t)
  }, [sseAlive])

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
      } catch (e) { toast(errText(e, '创建项目失败'), 'error') }
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

  const gateConfirm = async (decision: 'approved' | 'rejected', feedback?: string) => {
    const info = projects.find(p => p.id === activePid)
    if (!info) return
    try {
      const r: any = await api.gateConfirm(info.id, info.phase, decision, feedback)
      // 打回后要重跑调研/架构，而"点火"可能失败（该项目已有阶段在跑）——
      // 后端会回一个 warning。**必须让人看见**：不说的话，项目停着不动、
      // 界面只显示"调研中"，用户会以为它在跑（防御模式 #28）。
      if (r?.warning) toast(r.warning, 'info')
      fetchProjects(); fetchTasks()
    }
    catch (e) { toast(errText(e, '操作失败'), 'error') }
  }

  const { completed, failed, active } = useMemo(() => {
    let c = 0, f = 0, a = 0
    for (const t of tasks) {
      // ⚠️ **判据只有一份**（`TaskCard.tsx` 的 `taskStateKind`）。
      // 原来这里自己写了一遍 `failed || cancelled` —— 而后端**根本没有 `cancelled`**
      // （取消走 `rolled_back`）⇒ `rolled_back`（终态）被算进"执行中"，
      // 于是**有任务已经回滚了，顶上还显示在跑**（2026-09-14，外派④抓出）。
      const k = taskStateKind(t.status)
      if (k === 'done') c++
      else if (k === 'failed') f++
      else a++      // running / waiting 都还没结束
    }
    return { completed: c, failed: f, active: a }
  }, [tasks])

  const info = activePid !== '_default' ? projects.find(p => p.id === activePid) : null
  const gatePhase = info?.phase || ''
  const isGate = gatePhase.startsWith('gate')

  // 这个项目的用量 —— 2026-09-17 用户提：「看不到单独项目 token 用量，
  // 我建议放在独立项目对话框」。数据一直在（`/api/token-usage` 的 `by_project`），
  // 只是**界面一处都没读过**（`project_cost` 接口同理，前端 grep 零命中）。
  //
  // ⚠️ **这是「今日」的用量**（后端 `per_project_usage` 只汇总今天）——
  //    所以标题里要写明"今日"，不然昨天跑完的项目今天显示 0，看着像坏了。
  // ⚠️ 只在**换项目 / 换阶段**时取，不跟着 SSE 每一跳刷新。
  const [usage, setUsage] = useState<any>(null)
  useEffect(() => {
    if (!activePid || activePid === '_default') { setUsage(null); return }
    let dead = false
    api.tokenUsage()
      .then((d: any) => {
        if (dead) return
        const row = (d?.by_project || []).find((r: any) => r.project_id === activePid)
        setUsage(row ? { ...row, unpriced: (d?.unpriced_models || []).length > 0 } : null)
      })
      .catch(() => { if (!dead) setUsage(null) })
    return () => { dead = true }
  }, [activePid, gatePhase])
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
          {/* ── 常驻状态条（2026-09-17）────────────────────────────────
              用户提：「之前忽略了观察者对话窗口，导致现在调研架构等任务都堆在对话窗口」。
              查下来根子是**两种性质不同的东西共用了一条时间轴**：
                · 对话   = 流水（只增不减，要能往回翻）
                · 项目状态 = 快照（永远只有当前这一份，要能**一眼看到**）
              塞在一起必然打架：快照被流水推走、流水被快照截断。
              ⇒ 状态**钉在顶部不参与滚动**，材料收进侧滑面板，正文留给观察者说话。

              🔴 **门禁那根条必须在这里**：门禁是「当前状态 + 一个动作」，本来就不属于正文。
                 留在正文里的话，用户往下滚一屏就看不见"该我审批了"（#28 那一族）。
                 那个打回理由输入框也跟着搬 —— 不然点了"打回"，输入框在屏幕外。 */}
          {info && (
            <div style={{ flexShrink: 0, borderBottom: '1px solid #e5e2d8', background: '#fffdf8' }}>
              <div style={{ padding: '6px 12px', display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <span className="fs-12 fw-600">{info.name}</span>
                <span className="fs-11" style={{ color: gatePhase === 'done' ? '#16a34a' : '#6b6b68' }}>
                  {PHASE_NAMES[gatePhase] || gatePhase}
                </span>
                {tasks.length > 0 && (
                  <span className="fs-11 text-muted">
                    · {failed > 0 && <span style={{ color: '#dc2626' }}>⚠ </span>}
                    {/* ⚠️ 带上 `已完成/总数` —— 只说"1 个执行中"看不出还剩几个，
                        而这条摘要就是人在顶栏能看到的全部进度。 */}
                    {active > 0 ? `${active} 个执行中 · ${completed}/${tasks.length}`
                      : completed === tasks.length ? '全部完成' : `进度 ${completed}/${tasks.length}`}
                    {failed > 0 && <span style={{ color: '#dc2626' }}> {failed} 失败</span>}
                  </span>
                )}
                {usage && (
                  <span className="fs-11 text-muted" title="按项目汇总的**今日**用量；观察者对话单独算，不在里面">
                    · {fmtTokens(usage.tokens)} tokens · ${usage.cost.toFixed(4)}{usage.unpriced ? '+' : ''}（今日）
                  </span>
                )}
                <button onClick={() => setShowMaterials(v => !v)}
                  style={{ marginLeft: 'auto', background: showMaterials ? '#eef2ff' : '#f5f4ef',
                           border: '1px solid #e5e2d8', borderRadius: 6, padding: '2px 10px',
                           fontSize: 11, cursor: 'pointer' }}>
                  📁 材料
                </button>
              </div>
              {isGate && (
                <div style={{ padding: '0 12px 8px' }}>
                  <GateBar info={info} gateNum={gateNum} gatePhase={gatePhase} onGate={gateConfirm} />
                </div>
              )}
            </div>
          )}

          <div ref={scrollRef} onScroll={onScroll} style={{ flex: 1, overflow: 'auto', padding: '8px 0' }}>
            {msgs.map((m: ChatMsg, i: number) => <MessageBubble key={i} m={m} />)}

            {/* ⚠️ 留在正文里（不进材料面板）：它是"你看到的是旧数据"的免责声明 */}
            {!sseAlive && tasks.length > 0 && (
              <div className="chat-msg-row fs-10" style={{ color: 'var(--warning, #d48806)', marginBottom: 4 }}>
                ⚠ 实时连接断开 —— 任务状态是**轮询拿的**（10 秒一次），可能比实际慢一拍；正在自动重连
              </div>
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
              {/* 项目名/阶段/用量已搬到顶部**常驻状态条**（2026-09-17）——
                  放这儿的话会随正文滚走，而"现在到哪了"恰恰是要**一直看得见**的东西。 */}
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
      {/* ── 材料侧滑面板（2026-09-17）────────────────────────────────
          任务卡 / 门禁材料 / 调研 / 架构都在这。**默认收起**，正文留给观察者说话。
          ⚠️ **覆盖式**（而不是把正文挤窄）：否则对话的滚动位置会跳，
             而且窄屏上会被挤成一条缝。点右上角 ✕ 或「📁 材料」关掉。 */}
      {showMaterials && !isEmpty && (
        <div style={{ position: 'absolute', inset: 0, zIndex: 30 }}>
          <div onClick={() => setShowMaterials(false)}
               style={{ position: 'absolute', inset: 0, background: 'rgba(20,20,19,.12)' }} />
          <div style={{ position: 'absolute', top: 0, right: 0, bottom: 0,
                        width: 'min(560px, 92%)', background: '#ffffff',
                        borderLeft: '1px solid #e5e2d8', boxShadow: '-10px 0 30px rgba(0,0,0,.10)',
                        display: 'flex', flexDirection: 'column' }}>
            <div style={{ flexShrink: 0, padding: '8px 12px', borderBottom: '1px solid #e5e2d8',
                          display: 'flex', alignItems: 'center', gap: 8 }}>
              <b className="fs-12">📁 项目材料</b>
              {info && <span className="fs-11 text-muted">· {info.name}</span>}
              <button onClick={() => setShowMaterials(false)} aria-label="关闭材料面板"
                style={{ marginLeft: 'auto', background: 'none', border: 'none',
                         fontSize: 15, cursor: 'pointer', color: '#6b6b68' }}>✕</button>
            </div>
            <div style={{ flex: 1, overflow: 'auto', padding: 12 }}>
              {/* **按阶段分组**（2026-09-17 用户提：「每个阶段的任务都收纳到抽屉」）。
                  材料本来就是按阶段长出来的：调研出报告、架构出方案、实现出任务、交付出验收。
                  平铺的话，"这条属于哪个阶段"要靠读内容猜。 */}
              {info && <ProjectMaterials info={info} tasks={tasks} gateNum={gateNum}
                                         acceptance={acceptance}
                                         onRetry={retryFailed} onReveal={revealFile} />}
            </div>
          </div>
        </div>
      )}

      <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
    </div>
    {showFiles && <FilePanel onClose={() => setShowFiles(false)} />}
    </div>
  )
}
