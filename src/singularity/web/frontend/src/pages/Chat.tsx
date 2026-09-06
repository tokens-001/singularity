import { useState, useRef, useEffect } from 'react'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { useAppStore, type ChatMsg } from '../stores/app'
import { useToast } from '../components/Toast'
import { Send, Loader2, CheckCircle2, XCircle, RotateCcw, FolderOpen, ArrowUp, ChevronDown, FileText } from 'lucide-react'
import FilePanel from '../components/FilePanel'

interface ProgressItem { id: string; desc: string; status: string; ts: number; route_type?: string; duration?: number; error?: string; files?: string[]; verdict?: string }

export default function Chat() {
  const conversations = useAppStore(s => s.conversations)
  const activePid = useAppStore(s => s.activeProjectId)
  const setActiveProject = useAppStore(s => s.setActiveProject)
  const addChatMsg = useAppStore(s => s.addChatMsg)
  const msgs = conversations[activePid] || []

  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [showFiles, setShowFiles] = useState(false)
  const [execMode, setExecMode] = useState<'auto_edit'|'confirm_changes'>('auto_edit')
  const [tasks, setTasks] = useState<ProgressItem[]>([])
  const [projects, setProjects] = useState<any[]>([])
  const [status, setStatus] = useState<any>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const pendingCid = useRef<string>('')
  const projectsRef = useRef<any[]>([])
  const traceCache = useRef<Map<string, { files: string[]; verdict: string }>>(new Map())
  const toast = useToast(s => s.add)

  useEffect(() => {
    useAppStore.getState().clearConversation('_default')
    fetchProjects(); fetchTasks(); fetchStatus()
  }, [])
  useEffect(() => { requestAnimationFrame(() => { bottomRef.current?.scrollIntoView({behavior:'smooth'}) }) }, [msgs, tasks])

  const fetchStatus = async () => {
    try { setStatus(await api.status()) } catch {}
  }
  const pickRoot = async () => {
    try {
      const d: any = await api.fsPick()
      if (d?.path) { await api.setProjectsRoot(d.path); fetchStatus(); toast('项目根目录已设置', 'success') }
    } catch {}
  }
  const fetchProjects = async () => {
    try { const d: any = await api.projects(); const list = Array.isArray(d)?d:(d?.projects||[]); setProjects(list); projectsRef.current = list } catch { toast('加载项目失败', 'error') }
  }
  const fetchTasks = async () => {
    try {
      const t = await api.tasks()
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
            }).catch(() => {})
          }
        })
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
      const r = await api.observerChat(q, execMode, activePid !== '_default' ? activePid : '')
      if (r.client_id) { pendingCid.current = r.client_id }
      else if (r.answer) { addChatMsg({role:'assistant',content:r.answer,ts:Date.now()}); setLoading(false) }
    } catch { addChatMsg({role:'assistant',content:'请求失败，请确认后端服务在运行。',ts:Date.now()}); setLoading(false) }
  }

  const completed = tasks.filter(t => t.status === 'done').length
  const failed = tasks.filter(t => t.status === 'failed').length
  const active = tasks.filter(t => !['done','failed','cancelled'].includes(t.status)).length
  const info = activePid !== '_default' ? projects.find(p => p.id === activePid) : null
  const gatePhase = info?.phase || ''
  const isGate = gatePhase.startsWith('gate')
  const gateNum = isGate ? gatePhase.replace('gate','') : ''
  const gateLabels: Record<string,string> = { '1':'定义完成·请审核PRD', '2':'架构完成·请审核方案', '3':'验收完成·请审核交付物' }
  const phaseNames: Record<string,string> = { template:'待开始', researching:'调研中', planning:'架构设计中', executing:'实现中', integrating:'集成合并中', reviewing:'审查中', delivering:'交付中', done:'已完成' }
  const hasMsgs = msgs.length > 0
  const isEmpty = activePid === '_default'

  return (
    <div style={{ display: 'flex', height: '100%', flex: 1 }}>
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', flex: 1, position: 'relative' }}>

      {isEmpty ? (
        <div className="chat-empty">
          <div style={{ width: '100%', maxWidth: 680 }} className="chat-input-wrap">
            <textarea value={input} onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
              placeholder="描述你的项目或任务，回车开始…" rows={1} className="chat-textarea"/>
            <button onClick={send} disabled={!input.trim()} className="chat-send-btn"
              style={{ background: input.trim() ? '#141413' : '#e5e2d8', color: input.trim() ? '#fff' : '#9a9993', cursor: input.trim() ? 'pointer' : 'default', boxShadow: input.trim() ? '0 2px 8px rgba(20,20,19,0.25)' : 'none', transition: 'background 0.15s' }}>
              <ArrowUp size={16}/>
            </button>
          </div>

          <div style={{ width: '100%', maxWidth: 680, display: 'flex', alignItems: 'center', gap: 10, marginTop: 12, flexWrap: 'wrap', justifyContent: 'space-between' }}>
            {status?.workdir && (
              <div onClick={pickRoot} title="项目保存位置（点击更改）" style={{ display: 'flex', alignItems: 'center', gap: 6, background: '#fff', border: '1px solid #e5e2d8', borderRadius: 999, padding: '5px 14px', fontSize: 12, color: '#6b6b68', maxWidth: 340, cursor: 'pointer' }}>
                <FolderOpen size={13} color="#9a9993" style={{ flexShrink: 0 }}/>
                <span className="truncate">{status.workdir}</span>
              </div>
            )}
            <div style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}>
              <select value={execMode} onChange={e => setExecMode(e.target.value as any)}
                style={{ appearance: 'none', WebkitAppearance: 'none', background: '#fff',
                  border: '1px solid #e5e2d8', borderRadius: 999, padding: '5px 30px 5px 14px',
                  fontSize: 12, color: '#6b6b68', cursor: 'pointer', outline: 'none', lineHeight: 1.4 }}>
                <option value="auto_edit">⚡ 自动编辑</option>
                <option value="confirm_changes">🔒 逐步确认</option>
              </select>
              <ChevronDown size={13} color="#9a9993" style={{ position: 'absolute', right: 11, pointerEvents: 'none' }}/>
            </div>
          </div>

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
                  {active > 0 ? <Loader2 size={10} style={{animation:'spin 1s linear infinite'}}/> : <CheckCircle2 size={10} style={{color:'#16a34a'}}/>}
                  <span className="fw-600 fs-11 text-muted">
                    {active > 0 ? `${active} 个执行中` : completed === tasks.length ? '全部完成' : `进度 ${completed}/${tasks.length}`}
                    {failed > 0 && <span style={{ color: '#dc2626', marginLeft: 4 }}>{failed} 失败</span>}
                  </span>
                </div>
                {tasks.map((t) => {
                  const done = t.status === 'done'; const fail = t.status === 'failed' || t.status === 'cancelled'
                  return (
                    <div key={t.id} style={{ display: 'flex', gap: 10, alignItems: 'flex-start', padding: '10px 12px',
                      background: '#fff', border: '1px solid var(--border)', borderRadius: 10, boxShadow: 'var(--shadow-sm)', marginBottom: 6 }}>
                      <span style={{ flexShrink: 0, marginTop: 1 }}>
                        {done ? <CheckCircle2 size={16} color="#16a34a"/> : fail ? <XCircle size={16} color="#dc2626"/> : <Loader2 size={16} style={{ animation: 'spin 1s linear infinite', color: '#2563eb' }}/>}
                      </span>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <span className="truncate" style={{ flex: 1, fontSize: 12, fontWeight: 600, color: '#141413' }}>{t.desc.split('\n')[0]}</span>
                          <span style={{ flexShrink: 0, fontSize: 10, fontWeight: 600, padding: '1px 8px', borderRadius: 999,
                            background: done ? '#eaf6ec' : fail ? '#fdeaea' : '#eef2ff',
                            color: done ? '#16a34a' : fail ? '#dc2626' : '#2563eb' }}>
                            {done ? '完成' : fail ? '失败' : '执行中'}
                          </span>
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 3, fontSize: 11, color: '#9a9993' }}>
                          {t.route_type && <span>路由 {t.route_type}</span>}
                          {t.duration != null && <span>{t.duration}s</span>}
                          {t.error && <span style={{ color: '#dc2626' }}>{t.error}</span>}
                          {fail && (
                            <button onClick={e => { e.stopPropagation(); retryFailed(t.id) }}
                              style={{ background: 'none', border: 'none', color: '#2563eb', cursor: 'pointer', fontSize: 11, padding: 0 }}><RotateCcw size={11}/> 重试</button>
                          )}
                        </div>
                        {done && (t.files || []).length > 0 && (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 3, marginTop: 6 }}>
                            {(t.files || []).map(f => (
                              <div key={f} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, fontFamily: 'var(--font-mono)', padding: '3px 8px', background: '#f3f2ec', border: '1px solid var(--border)', borderRadius: 6, color: '#6b6b68', maxWidth: '100%' }}>
                                <FileText size={12} color="#9a9993" style={{ flexShrink: 0 }}/>
                                <span className="truncate" style={{ flex: 1 }}>{f}</span>
                                <button onClick={e => { e.stopPropagation(); api.revealFile(f).catch(() => toast('定位失败', 'error')) }}
                                  title="在文件夹中显示" aria-label="在文件夹中显示"
                                  style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 1, display: 'flex', color: '#9a9993', flexShrink: 0 }}>
                                  <FolderOpen size={13}/>
                                </button>
                              </div>
                            ))}
                            {t.verdict && t.verdict !== '?' && <span style={{ fontSize: 10, fontWeight: 600, padding: '2px 8px', borderRadius: 6, background: '#eaf6ec', color: '#16a34a', width: 'fit-content' }}>✓ {t.verdict}</span>}
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}

            {isGate && info && (
              <div style={{ padding:'8px 0', textAlign:'center' }}>
                <div style={{ display:'inline-flex',alignItems:'center',gap:8,background:'#eaf6ec',border:'1px solid #16a34a',borderRadius:8,padding:'8px 16px' }}>
                  <span style={{fontSize:13,fontWeight:600,color:'#16a34a'}}>🛑 GATE{gateNum}</span>
                  <span style={{fontSize:12,color:'#6b6b68'}}>{gateLabels[gateNum] || '等待审核'}</span>
                  <button onClick={async e=>{e.stopPropagation();try{await api.gateConfirm(info.id,gatePhase,'approved');fetchProjects();fetchTasks()}catch{toast('操作失败','error')}}}
                    style={{background:'#16a34a',color:'#141413',border:'none',borderRadius:4,padding:'3px 10px',fontSize:11,fontWeight:600,cursor:'pointer'}}>✅ 通过</button>
                  <button onClick={async e=>{e.stopPropagation();try{await api.gateConfirm(info.id,gatePhase,'rejected');fetchProjects();fetchTasks()}catch{toast('操作失败','error')}}}
                    style={{background:'#b5b2a8',color:'#dc2626',border:'none',borderRadius:4,padding:'3px 10px',fontSize:11,cursor:'pointer'}}>↩ 打回</button>
                </div>
                <div style={{ maxWidth: 760, margin: '12px auto 0', textAlign: 'left' }}>
                  {info.research_report && (
                    <details style={{ background: '#ffffff', border: '1px solid #e5e2d8', borderRadius: 10, marginBottom: 10, overflow: 'hidden' }}>
                      <summary style={{ cursor: 'pointer', padding: '12px 14px', fontSize: 13, fontWeight: 700, color: '#2563eb', listStyle: 'none', display: 'flex', alignItems: 'center', gap: 8, userSelect: 'none' }}>
                        <span>📋 调研报告</span>
                        <span style={{ marginLeft: 'auto', color: '#6b6b68', fontSize: 11, fontWeight: 400 }}>点击展开 ▾</span>
                      </summary>
                      <div style={{ padding: '0 14px 14px' }}>
                        {info.research_report.recommendation && (
                          <div style={{ marginBottom: 10 }}>
                            <div style={{ fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 }}>推荐方案</div>
                            <div style={{ fontSize: 12, color: '#141413', lineHeight: 1.6 }}>{info.research_report.recommendation}</div>
                          </div>
                        )}
                        {(info.research_report.competitive_analysis?.products || []).length > 0 && (
                          <div style={{ marginBottom: 10 }}>
                            <div style={{ fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 }}>竞品分析</div>
                            {(info.research_report.competitive_analysis?.products || []).map((p: any, i: number) => (
                              <div key={i} style={{ fontSize: 12, color: '#141413', padding: '6px 8px', background: '#faf9f5', borderRadius: 6, marginBottom: 4, lineHeight: 1.5 }}>
                                <b style={{ color: '#141413' }}>{p.name}</b> <span style={{ color: '#6b6b68' }}>· {p.type}</span><br/>
                                <span style={{ color: '#16a34a' }}>优：</span>{p.strengths}
                              </div>
                            ))}
                          </div>
                        )}
                        {(info.research_report.pitfalls || []).length > 0 && (
                          <div>
                            <div style={{ fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 }}>关键坑</div>
                            {(info.research_report.pitfalls || []).map((p: string, i: number) => (
                              <div key={i} style={{ fontSize: 12, color: '#dc2626', lineHeight: 1.5, marginBottom: 2 }}>⚠ {p}</div>
                            ))}
                          </div>
                        )}
                      </div>
                    </details>
                  )}
                  {info.architecture && (
                    <details style={{ background: '#ffffff', border: '1px solid #e5e2d8', borderRadius: 10, overflow: 'hidden' }}>
                      <summary style={{ cursor: 'pointer', padding: '12px 14px', fontSize: 13, fontWeight: 700, color: '#7c3aed', listStyle: 'none', display: 'flex', alignItems: 'center', gap: 8, userSelect: 'none' }}>
                        <span>🏗 架构方案</span>
                        <span style={{ marginLeft: 'auto', color: '#6b6b68', fontSize: 11, fontWeight: 400 }}>点击展开 ▾</span>
                      </summary>
                      <div style={{ padding: '0 14px 14px' }}>
                        {info.architecture.architecture && (
                          <div style={{ fontSize: 12, color: '#141413', lineHeight: 1.6, marginBottom: 10, padding: '8px 10px', background: '#faf9f5', borderRadius: 6 }}>
                            {info.architecture.architecture}
                          </div>
                        )}
                        {(info.architecture.modules || []).length > 0 && (
                          <div style={{ marginBottom: 10 }}>
                            <div style={{ fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 6 }}>模块（{(info.architecture.modules || []).length}）</div>
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                              {(info.architecture.modules || []).map((m: any, i: number) => (
                                <span key={i} style={{ fontSize: 11, color: '#141413', background: '#faf9f5', border: '1px solid #e5e2d8', borderRadius: 6, padding: '3px 9px' }}>{m.name}</span>
                              ))}
                            </div>
                          </div>
                        )}
                        {(info.architecture.tasks || []).length > 0 && (
                          <div>
                            <div style={{ fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 }}>任务（{(info.architecture.tasks || []).length}）</div>
                            {(info.architecture.tasks || []).map((t: any, i: number) => (
                              <div key={i} style={{ fontSize: 11, color: '#141413', padding: '4px 0', borderBottom: '1px solid #f3f2ec' }}>
                                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                                  <span style={{ color: '#6b6b68', fontFamily: 'monospace' }}>{t.id}</span>
                                  <span style={{ flex: 1 }}>{t.title}</span>
                                  <span style={{ fontSize: 10, color: t.complexity === 'high' ? '#dc2626' : t.complexity === 'medium' ? '#b45309' : '#16a34a' }}>{t.complexity}</span>
                                </div>
                                {(t.depends_on?.length > 0 || t.acceptance) && (
                                  <div style={{ fontSize: 10, color: '#6b6b68', marginTop: 2 }}>
                                    {t.depends_on?.length > 0 && <span>依赖：{t.depends_on.join(', ')}</span>}
                                    {t.depends_on?.length > 0 && t.acceptance && <span> · </span>}
                                    {t.acceptance && <span>验收：{t.acceptance}</span>}
                                  </div>
                                )}
                              </div>
                            ))}
                          </div>
                        )}
                        {(info.architecture.data_model?.entities || []).length > 0 && (
                          <div style={{ marginBottom: 10 }}>
                            <div style={{ fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 }}>数据模型（{(info.architecture.data_model?.entities || []).length}）</div>
                            {(info.architecture.data_model?.entities || []).map((e: any, i: number) => (
                              <div key={i} style={{ fontSize: 11, color: '#141413', padding: '4px 8px', background: '#faf9f5', borderRadius: 6, marginBottom: 4 }}>
                                <b style={{ color: '#141413' }}>{e.name}</b> <span style={{ color: '#6b6b68' }}>（{e.fields?.length || 0} 字段）</span> {(e.fields || []).map((f: any) => f.name).join(', ')}
                              </div>
                            ))}
                            {(info.architecture.data_model?.relationships || []).length > 0 && (
                              <div style={{ fontSize: 11, color: '#6b6b68', lineHeight: 1.6 }}>
                                <b>关系：</b>{(info.architecture.data_model?.relationships || []).map((r: any, i: number) => (
                                  <span key={i}>{r.from}→{r.to}({r.type}) </span>
                                ))}
                              </div>
                            )}
                          </div>
                        )}
                        {(info.architecture.api_contracts || []).length > 0 && (
                          <div>
                            <div style={{ fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 }}>API 契约（{(info.architecture.api_contracts || []).length}）</div>
                            {(info.architecture.api_contracts || []).map((a: any, i: number) => (
                              <div key={i} style={{ fontSize: 11, color: '#141413', padding: '3px 0', borderBottom: '1px solid #f3f2ec' }}>
                                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                                  <span style={{ color: '#16a34a', fontFamily: 'monospace', fontWeight: 600, minWidth: 36 }}>{a.method}</span>
                                  <span style={{ color: '#2563eb', fontFamily: 'monospace' }}>{a.path}</span>
                                  <span style={{ color: '#6b6b68', flex: 1 }}>{a.description}</span>
                                </div>
                                {(a.input || a.output) && (
                                  <div style={{ fontSize: 10, color: '#6b6b68', paddingLeft: 44, marginTop: 2 }}>
                                    {a.input && <div>入参：{JSON.stringify(a.input)}</div>}
                                    {a.output && <div>返回：{JSON.stringify(a.output)}</div>}
                                  </div>
                                )}
                              </div>
                            ))}
                          </div>
                        )}
                        {info.architecture.tech_stack && (
                          <div style={{ marginBottom: 10 }}>
                            <div style={{ fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 }}>技术栈</div>
                            {Object.entries(info.architecture.tech_stack).map(([k, v]: [string, any], i: number) => (
                              <div key={i} style={{ fontSize: 11, color: '#141413', padding: '3px 0' }}>
                                <b style={{ color: '#6b6b68' }}>{k}：</b>{v}
                              </div>
                            ))}
                          </div>
                        )}
                        {(info.architecture.constraints || []).length > 0 && (
                          <div style={{ marginBottom: 10 }}>
                            <div style={{ fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 }}>约束（{(info.architecture.constraints || []).length}）</div>
                            {(info.architecture.constraints || []).map((c: any, i: number) => (
                              <div key={i} style={{ fontSize: 11, color: '#141413', padding: '3px 0', borderBottom: '1px solid #f3f2ec' }}>
                                <span style={{ color: '#b45309', fontWeight: 600 }}>[{c.type}]</span> {c.rule}
                                {c.check && <span style={{ color: '#6b6b68' }}> → 验证：{c.check}</span>}
                              </div>
                            ))}
                          </div>
                        )}
                        {(info.architecture.risks || []).length > 0 && (
                          <div>
                            <div style={{ fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 }}>风险（{(info.architecture.risks || []).length}）</div>
                            {(info.architecture.risks || []).map((r: any, i: number) => (
                              <div key={i} style={{ fontSize: 11, color: '#141413', padding: '3px 0', borderBottom: '1px solid #f3f2ec' }}>
                                <span style={{ color: r.impact === 'high' ? '#dc2626' : r.impact === 'medium' ? '#b45309' : '#16a34a', fontWeight: 600 }}>[{r.impact}]</span> {r.risk}
                                {r.mitigation && <span style={{ color: '#6b6b68' }}> → {r.mitigation}</span>}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    </details>
                  )}
                </div>
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
              {info && <span className="fs-11 text-muted" style={{ whiteSpace: 'nowrap' }}>
                {info.name} <span style={{color: isGate?'#16a34a':gatePhase==='done'?'#16a34a':'#9a9993'}}>· {phaseNames[gatePhase] || gatePhase}</span>
              </span>}
              <textarea value={input} onChange={e => setInput(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
                placeholder="发送消息..." rows={1} className="chat-textarea"/>
              <button onClick={() => setShowFiles(!showFiles)} className="btn-icon" style={{ color: showFiles?'#2563eb':'#9a9993' }}>
                <FolderOpen size={15}/>
              </button>
              <button onClick={send} disabled={!input.trim()} className="chat-send-btn"
                style={{ background: input.trim() ? '#141413' : '#e5e2d8', color: input.trim() ? '#fff' : '#9a9993', cursor: input.trim() ? 'pointer' : 'default' }}>
                <ArrowUp size={14}/>
              </button>
            </div>
            <div style={{ maxWidth: 860, margin: '6px auto 0', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', justifyContent: 'space-between' }}>
              {status?.workdir && (
                <div onClick={pickRoot} title="项目保存位置（点击更改）" style={{ display: 'flex', alignItems: 'center', gap: 6, background: '#fff', border: '1px solid #e5e2d8', borderRadius: 999, padding: '5px 14px', fontSize: 12, color: '#6b6b68', maxWidth: 340, cursor: 'pointer' }}>
                  <FolderOpen size={13} color="#9a9993" style={{ flexShrink: 0 }}/>
                  <span className="truncate">{status.workdir}</span>
                </div>
              )}
              <div style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}>
                <select value={execMode} onChange={e => setExecMode(e.target.value as any)}
                  style={{ appearance: 'none', WebkitAppearance: 'none', background: '#fff',
                    border: '1px solid #e5e2d8', borderRadius: 999, padding: '5px 30px 5px 14px',
                    fontSize: 12, color: '#6b6b68', cursor: 'pointer', outline: 'none', lineHeight: 1.4 }}>
                  <option value="auto_edit">⚡ 自动编辑</option>
                  <option value="confirm_changes">🔒 逐步确认</option>
                </select>
                <ChevronDown size={13} color="#9a9993" style={{ position: 'absolute', right: 11, pointerEvents: 'none' }}/>
              </div>
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
