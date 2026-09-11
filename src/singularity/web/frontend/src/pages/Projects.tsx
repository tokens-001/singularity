import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { useToast, useModal, useRun, errText } from '../lib/toast'
import { getPinned, togglePin } from '../lib/pinned'
import { useAppStore } from '../stores/app'
import { ResearchReport, ArchitectureDetails } from '../components/chat/GatePanel'
import { Plus, RefreshCw, ChevronDown, ChevronRight, Pin, Trash2 } from 'lucide-react'

const PHASE_CN: Record<string,string> = {
  template:'模板', researching:'调研', gate1:'G1确认', planning:'规划', gate2:'G2确认',
  executing:'执行', integrating:'集成', reviewing:'审查', fixing:'修复', gate3:'G3确认', delivering:'交付', done:'完成'
}
const PC: Record<string,string> = {
  template:'var(--text-muted)', researching:'var(--accent)', gate1:'var(--accent-yellow)', planning:'var(--accent-purple)',
  gate2:'var(--accent-yellow)', executing:'var(--accent-green)', integrating:'var(--accent-green)',
  reviewing:'#ea580c', gate3:'var(--accent-yellow)', delivering:'var(--accent)', done:'var(--accent-green)'
}
const PHASES = ['template','researching','gate1','planning','gate2','executing','integrating','reviewing','gate3','delivering','done']

/** 12 个阶段铺满一行太吵 —— 压成一条进度条（阶段名上面那行已经有了） */
function PhaseBar({ phase }: { phase: string }) {
  const i = PHASES.indexOf(phase) + 1
  return (
    <div className="flex-center gap-6" style={{ marginBottom: 10 }}>
      <div style={{ flex: 1, height: 3, background: 'var(--border)', borderRadius: 2 }}>
        <div style={{ width: `${(i / PHASES.length) * 100}%`, height: '100%', borderRadius: 2,
          background: PC[phase] || 'var(--accent)' }}/>
      </div>
      <span className="fs-10 text-muted" style={{ flexShrink: 0 }}>{i}/{PHASES.length}</span>
    </div>
  )
}

export default function Projects() {
  const [projects, setProjects] = useState<any[]>([])
  const [expanded, setExpanded] = useState<string|null>(null)
  const detailSeq = useRef(0)   // toggle 的请求序号，用于丢弃过期详情
  const [detail, setDetail] = useState<any>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [form, setForm] = useState({ name: '', description: '', template: 'feature' })
  const [loading, setLoading] = useState(true)
  const [pinned, setPinned] = useState<string[]>(getPinned)
  const toast = useToast()
  const modal = useModal()
  const run = useRun()
  const activePid = useAppStore(s => s.activeProjectId)
  const setActiveProject = useAppStore(s => s.setActiveProject)
  const navigate = useNavigate()

  const fetch = async () => {
    setLoading(true)
    try { const d: any = await api.projects(); setProjects(Array.isArray(d)?d:(d?.projects||[])) } catch (e) { toast(errText(e, '加载项目失败'), 'error') }
    setLoading(false)
  }
  useEffect(() => { fetch() }, [])
  // project=新建/删除项目；workflow=阶段流转（phase 变化）；task/system 兜底
  useSSE(() => { fetch() }, { kinds: ['project', 'workflow', 'task', 'system'], debounceMs: 400 })

  const toggle = async (id: string) => {
    if (expanded === id) { setExpanded(null); setDetail(null); return }
    setExpanded(id)
    // 请求序号：快速点开 A 再点 B 时，A 的详情后到会盖掉 B 的 ——
    // 表现为展开区显示 A 的阶段条/调研报告，标题却是 B。
    // （同族修复见 Chat.tsx 的 fetchSeq。）
    const seq = ++detailSeq.current
    try {
      const d = await api.project(id)
      if (seq !== detailSeq.current) return   // 已经切到别的项目了，丢弃
      setDetail(d)
    } catch (e) { toast(errText(e, '加载项目详情失败'), 'error') }
  }

  const create = async () => {
    if (!form.name) return
    if (!(await run(() => api.createProject(form)))) return
    setShowCreate(false); setForm({ name: '', description: '', template: 'feature' }); fetch()
  }

  const del = (p: any) => modal.confirm({
    title: `删除项目「${p.name}」？`,
    content: '该操作不可撤销。',
    okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
    onOk: async () => {
      if (!(await run(() => api.deleteProject(p.id)))) return
      if (expanded === p.id) { setExpanded(null); setDetail(null) }
      if (activePid === p.id) setActiveProject('_default')   // 删的是当前项目 → 别让对话页停在死项目上
      fetch()
    },
  })

  const pin = (id: string) => { togglePin(id); setPinned(getPinned()) }

  return (
    <div className="page-wrap-wide">
      <div className="flex-center gap-8" style={{ marginBottom: 12 }}>
        <h2 className="page-title">项目</h2>
        <span className="fs-11 text-muted">{projects.length} 个</span>
        <span className="flex-1"/>
        <button onClick={fetch} className="btn-icon" aria-label="刷新"><RefreshCw size={14}/></button>
        <button onClick={()=>setShowCreate(!showCreate)} className="btn-green" style={{ padding: '6px 12px', fontSize: 12, gap: 4 }}><Plus size={14}/> 新建</button>
      </div>

      {showCreate && (
        <div className="flex-center gap-8 flex-wrap" style={{ marginBottom: 10, padding: 10, background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
          <input value={form.name} onChange={e=>setForm({...form,name:e.target.value})} placeholder="项目名称" className="inp-sm" style={{ flex: 1 }}/>
          <input value={form.description} onChange={e=>setForm({...form,description:e.target.value})} placeholder="需求描述" className="inp-sm" style={{ flex: 2 }}/>
          <select value={form.template} onChange={e=>setForm({...form,template:e.target.value})} className="inp-sm" style={{ width: 'auto' }}>
            <option value="feature">新功能</option><option value="bugfix">Bug修复</option><option value="refactor">重构</option><option value="test">写测试</option><option value="review">代码审查</option>
          </select>
          <button onClick={create} className="btn-green" style={{ padding: '6px 14px', fontSize: 12 }}>创建</button>
        </div>
      )}

      {loading ? (
        <div>
          {[1,2,3].map(i => <div key={i} className="skeleton skeleton-row"/>)}
        </div>
      ) : (
        <>
          {projects.map((p: any) => {
            const isPinned = pinned.includes(p.id)
            return (
              <div key={p.id} style={{ marginBottom: 6 }}>
                <div onClick={()=>toggle(p.id)}
                  className="flex-center gap-8" style={{ padding: '8px 10px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', cursor: 'pointer' }}>
                  <span className="text-muted">{expanded===p.id?<ChevronDown size={12}/>:<ChevronRight size={12}/>}</span>
                  <span className="fw-600 fs-13 flex-1">{p.name}</span>
                  <span className="fs-10 fw-600" style={{ color: PC[p.phase]||'var(--text-muted)' }}>{PHASE_CN[p.phase]||p.phase}</span>
                  <button onClick={e => { e.stopPropagation(); navigate(`/tasks?project=${p.id}`) }}
                    title="只看该项目的任务"
                    style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '0 2px',
                      fontSize: 10, color: '#6b6b68', textDecoration: 'underline' }}>
                    {p.task_ids?.length || 0} 任务
                  </button>
                  <button onClick={e=>{e.stopPropagation(); pin(p.id)}} className="btn-icon"
                    title={isPinned?'取消置顶':'置顶'} aria-label={isPinned?'取消置顶':'置顶'}
                    style={{ color: isPinned?'#d97706':'#b5b2a8' }}><Pin size={12}/></button>
                  <button onClick={e=>{e.stopPropagation(); del(p)}} className="btn-icon"
                    title="删除项目" aria-label="删除项目" style={{ color:'#dc2626' }}><Trash2 size={12}/></button>
                </div>
                {expanded === p.id && detail && (
                  <div style={{ marginLeft: 20, padding: '8px 14px', borderLeft: '1px solid var(--border)', fontSize: 12 }}>
                    <PhaseBar phase={detail.phase} />
                    <div className="text-secondary" style={{ marginBottom: 4 }}>{detail.description}</div>
                    {detail.repo_dir && <div className="fs-10 text-muted" style={{ marginBottom: 4 }}>📁 成品：{detail.repo_dir}</div>}
                    {detail.research_report && <ResearchReport report={detail.research_report} />}
                    {detail.architecture && <ArchitectureDetails arch={detail.architecture} />}
                    {detail.phase && detail.phase.startsWith('gate') && (
                      <div className="flex-center gap-6 fs-10" style={{ marginBottom: 8, color: 'var(--accent-yellow)' }}>
                        🛑 {PHASE_CN[detail.phase]} — 等待审批
                        <button onClick={() => { setActiveProject(p.id); navigate('/') }} className="btn-sm">去对话页审批</button>
                      </div>
                    )}
                    {detail.lineage && detail.lineage.length > 0 && (
                      <div className="fs-10 text-muted" style={{ marginTop: 4 }}>
                        {detail.lineage.slice(-5).map((l:any,i:number) => (
                          <div key={i}>[{l.action}] {l.agent||''} {l.task_count?l.task_count+'任务':''}</div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
          {!loading && projects.length === 0 && (
            <div className="fs-11 text-muted" style={{ padding: 20, textAlign: 'center' }}>暂无项目，点"新建"创建</div>
          )}
        </>
      )}
    </div>
  )
}
