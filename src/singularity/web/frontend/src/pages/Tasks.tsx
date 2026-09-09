import { useState, useEffect, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, Task } from '../lib/api'
import { useSSE, useSSEConnected } from '../lib/useSSE'
import { useVirtualRows } from '../lib/useVirtualRows'
import { useToast, useModal, useRun } from '../lib/toast'
import { Plus, RefreshCw, RotateCcw, XCircle, Trash2, Search, Pause, Play, X } from 'lucide-react'

const STATUS_CN: Record<string,string> = { pending:'待处理', running:'进行中', done:'已完成', failed:'失败', blocked:'已暂停', paused:'已暂停' }
const STATUS_COLOR: Record<string,string> = { pending:'#9a9993', running:'#2563eb', done:'#16a34a', failed:'#dc2626', blocked:'#b45309', paused:'#b45309' }

export default function Tasks() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [projectNames, setProjectNames] = useState<Record<string,string>>({})
  const [showCreate, setShowCreate] = useState(false)
  const [desc, setDesc] = useState('')
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [params, setParams] = useSearchParams()
  const projectFilter = params.get('project') || ''   // 从项目页「N 任务」跳进来时带的项目 id
  const toast = useToast()
  const modal = useModal()
  const run = useRun()

  const fetch = useCallback(() => {
    setLoading(true)
    api.tasks().then(setTasks).catch(() => toast('加载任务失败', 'error')).finally(() => setLoading(false))
    api.projects().then((ps: any[]) => {
      const m: Record<string,string> = {}
      ps.forEach((p: any) => { if (p.id) m[p.id] = p.name || p.id })
      setProjectNames(m)
    }).catch(() => {})
  }, [])
  useEffect(() => { fetch() }, [fetch])
  const sseAlive = useSSEConnected()
  useSSE(() => { fetch() }, { kinds: ['task', 'tool:start', 'tool:done', 'system'], debounceMs: 400 })
  useEffect(() => {
    if (sseAlive) return   // SSE 活着 → 事件驱动；断了才退回轮询
    const t = setInterval(fetch, 10000); return () => clearInterval(t)
  }, [fetch, sseAlive])

  const create = async () => {
    if (!desc.trim()) return
    if (!(await run(() => api.createTask(desc)))) return
    setShowCreate(false); setDesc(''); fetch()
  }
  const act = async (fn: (id: string) => Promise<any>, id: string) => { if (await run(() => fn(id))) fetch() }
  const confirmDelete = (t: Task) => modal.confirm({
    title: '删除这个任务？',
    content: t.description.split('\n')[0],
    okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
    onOk: () => act(api.deleteTask, t.id),
  })

  const list = tasks.filter(t =>
    (!projectFilter || t.project_id === projectFilter) &&
    (!search || t.description.toLowerCase().includes(search.toLowerCase())))
  // 行高固定 36px + 间距 4px（单行截断，不换行）；列表短时 start/end 覆盖全部，占位为 0
  const V = useVirtualRows(list.length, 36, 4)

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div className="flex-center gap-8" style={{ marginBottom: 12 }}>
        <h2 className="fs-13 fw-600" style={{ color: '#141413' }}>任务</h2>
        <span className="fs-11 text-muted">{tasks.length} 个</span>
        {projectFilter && (
          <span className="flex-center gap-4 fs-10" style={{ padding: '2px 8px', borderRadius: 4, background: '#eef2ff', color: '#4f46e5', border: '1px solid #c7d2fe' }}>
            项目：{projectNames[projectFilter] || projectFilter.slice(0, 8)}
            <button onClick={() => setParams({})} aria-label="清除项目筛选"
              style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, color: 'inherit', display: 'flex' }}><X size={10}/></button>
          </span>
        )}
        <div className="search-box">
          <Search size={12} color="#9a9993"/>
          <input value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索..." aria-label="搜索任务" className="search-input"/>
        </div>
        <span className="flex-1"/>
        <button onClick={fetch} className="btn-icon" aria-label="刷新"><RefreshCw size={14}/></button>
        <button onClick={() => setShowCreate(!showCreate)} className="btn-white"><Plus size={12}/> 新建</button>
      </div>

      {showCreate && (
        <div className="flex-center gap-8" style={{ marginBottom: 10, padding: 8, background: '#f3f2ec', borderRadius: 8 }}>
          <input value={desc} onChange={e => setDesc(e.target.value)} placeholder="任务描述..." onKeyDown={e => e.key === 'Enter' && create()}
            className="inp-dark" style={{ flex: 1 }}/>
          <button onClick={create} style={{ background: '#fff', color: '#141413', border: 'none', borderRadius: 6, padding: '6px 14px', cursor: 'pointer', fontSize: 11, fontWeight: 600 }}>创建</button>
        </div>
      )}

      {loading ? (
        <div>{[1,2,3,4,5].map(i => <div key={i} className="skeleton skeleton-row"/>)}</div>
      ) : list.length === 0 ? (
        <div className="fs-11 text-muted" style={{ padding: 30, textAlign: 'center' }}>暂无任务，点「新建」创建一个</div>
      ) : (
        <div ref={V.ref} onScroll={V.onScroll} style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, paddingTop: V.padTop, paddingBottom: V.padBottom }}>
          {list.slice(V.start, V.end).map(t => (
            <div key={t.id} className="flex-center gap-8" title={t.description}
              style={{ padding: '8px 10px', background: '#fff', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12 }}>
              <span className="status-dot" style={{ background: STATUS_COLOR[t.status] || '#9a9993', flexShrink: 0 }}/>
              {t.project_id && (
                <span className="fs-10" style={{ flexShrink: 0, padding: '1px 6px', borderRadius: 4, background: '#eef2ff', color: '#4f46e5', border: '1px solid #c7d2fe', whiteSpace: 'nowrap' }}>
                  {projectNames[t.project_id] || t.project_id.slice(0, 8)}
                </span>
              )}
              <span className="truncate" style={{ flex: 1, color: '#141413' }}>{t.description.split('\n')[0]}</span>
              <span className="fs-10" style={{ color: STATUS_COLOR[t.status] || '#9a9993', flexShrink: 0 }}>{STATUS_CN[t.status] || t.status}</span>
              <span className="fs-10 text-muted mono" style={{ flexShrink: 0 }}>{t.id.slice(0, 8)}</span>
              <span className="flex-center gap-4">
                {t.status === 'failed' && <button onClick={() => act(api.retryTask, t.id)} className="btn-icon" title="重试" aria-label="重试"><RotateCcw size={12}/></button>}
                {t.status === 'running' && <button onClick={() => act(api.pauseTask, t.id)} className="btn-icon" title="暂停" aria-label="暂停"><Pause size={12}/></button>}
                {t.status === 'paused' && <button onClick={() => act(api.resumeTask, t.id)} className="btn-icon" title="恢复" aria-label="恢复"><Play size={12}/></button>}
                {['pending','running','paused'].includes(t.status) && <button onClick={() => act(api.cancelTask, t.id)} className="btn-icon" title="取消" aria-label="取消"><XCircle size={12}/></button>}
                <button onClick={() => confirmDelete(t)} className="btn-icon" title="删除" aria-label="删除" style={{ color: '#dc2626' }}><Trash2 size={12}/></button>
              </span>
            </div>
          ))}
          </div>
        </div>
      )}
    </div>
  )
}
