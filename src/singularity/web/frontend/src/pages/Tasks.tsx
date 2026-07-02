import { useState, useEffect, useCallback } from 'react'
import { api, Task } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { useToast } from '../components/Toast'
import { Play, Square, RotateCcw, XCircle, Plus, RefreshCw } from 'lucide-react'

const COLUMNS = [
  { key: 'pending', label: '待处理', color: '#666' },
  { key: 'running', label: '进行中', color: '#58a6ff' },
  { key: 'done', label: '已完成', color: '#3fb950' },
  { key: 'failed', label: '失败', color: '#f85149' },
  { key: 'blocked', label: '已暂停', color: '#d2991d' },
]

export default function Tasks() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [showCreate, setShowCreate] = useState(false)
  const [desc, setDesc] = useState('')
  const [expanded, setExpanded] = useState<string|null>(null)
  const [detail, setDetail] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [dragOver, setDragOver] = useState<string>('')
  const toast = useToast(s => s.add)

  const fetch = useCallback(() => {
    setLoading(true)
    api.tasks().then(setTasks).catch(() => toast('加载任务失败', 'error')).finally(()=>setLoading(false))
  }, [])
  useEffect(() => { fetch() }, [fetch])
  useSSE(() => { fetch() })
  useEffect(() => { const t=setInterval(fetch,10000); return ()=>clearInterval(t) }, [fetch])

  const toggle = async (id: string) => {
    if (expanded === id) { setExpanded(null); setDetail(null); return }
    setExpanded(id)
    try { const d = await api.task(id); setDetail(d) } catch { toast('加载任务详情失败', 'error'); setDetail(null) }
  }
  const create = () => { if (desc.trim()) { api.createTask(desc).then(()=>{setShowCreate(false);setDesc('');fetch()}) } }
  const act = (fn: (id:string)=>Promise<any>, id: string) => { fn(id).then(fetch) }

  const handleDragStart = (e: React.DragEvent, tid: string) => { e.dataTransfer.setData('taskId', tid) }
  const handleDrop = async (e: React.DragEvent, newStatus: string) => {
    e.preventDefault(); setDragOver('')
    const tid = e.dataTransfer.getData('taskId')
    const task = tasks.find(t => t.id === tid)
    if (!task || task.status === newStatus) return
    if (newStatus === 'pending') await api.retryTask(tid)
    else if (newStatus === 'running') await api.releaseTask(tid)
    else if (newStatus === 'done') await api.approveTask(tid)
    else if (newStatus === 'failed') await api.cancelTask(tid)
    else if (newStatus === 'blocked') await api.holdTask(tid)
    fetch()
  }

  const tasksByStatus = (status: string) => tasks.filter((t: Task) => t.status === status)

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div className="flex-center gap-8" style={{ marginBottom: 12 }}>
        <h2 style={{ fontSize: 15, fontWeight: 600, color: '#fff' }}>任务</h2>
        <span className="fs-11 text-muted">{tasks.length} 个</span>
        <span className="flex-1"/>
        <button onClick={fetch} className="btn-icon"><RefreshCw size={14}/></button>
        <button onClick={()=>setShowCreate(!showCreate)} className="btn-white"><Plus size={12}/> 新建</button>
      </div>

      {showCreate && (
        <div className="flex-center gap-8" style={{ marginBottom: 10, padding: 8, background: '#1c1c1e', borderRadius: 8 }}>
          <input value={desc} onChange={e=>setDesc(e.target.value)} placeholder="任务描述..." onKeyDown={e=>e.key==='Enter'&&create()}
            className="inp-dark" style={{ flex: 1 }}/>
          <button onClick={create} style={{ background: '#fff', color: '#000', border: 'none', borderRadius: 6, padding: '6px 14px', cursor: 'pointer', fontSize: 11, fontWeight: 600 }}>创建</button>
        </div>
      )}

      {loading ? (
        <div style={{ display: 'flex', gap: 10, flex: 1 }}>
          {COLUMNS.map(col => <div key={col.key} className="skeleton skeleton-card" style={{ flex: 1, minWidth: 170 }}/>)}
        </div>
      ) : (
        <div className="kanban">
          {COLUMNS.map(col => (
            <div key={col.key} className={`kanban-col ${dragOver === col.key ? 'kanban-col-hover' : ''}`}
              onDragOver={e => { e.preventDefault(); setDragOver(col.key) }}
              onDragLeave={() => setDragOver('')}
              onDrop={e => handleDrop(e, col.key)}>
              <div className="flex-center gap-6" style={{ marginBottom: 8 }}>
                <span style={{ width: 6, height: 6, borderRadius: 3, background: col.color, display: 'inline-block' }}/>
                <span className="fw-600 fs-11" style={{ color: col.color }}>{col.label}</span>
                <span className="fs-10" style={{ color: '#555' }}>{tasksByStatus(col.key).length}</span>
              </div>
              {tasksByStatus(col.key).map((t: Task) => (
                <div key={t.id} draggable onDragStart={e => handleDragStart(e, t.id)} onClick={() => toggle(t.id)}
                  className="kanban-card" style={{ borderLeft: `3px solid ${col.color}` }}>
                  <div className="truncate" style={{ marginBottom: 4, lineHeight: 1.4 }}>{t.description.split('\n')[0].slice(0, 80)}</div>
                  <div className="flex-center gap-4 fs-9" style={{ color: '#555' }}>
                    <span className="mono">{t.id.slice(0, 8)}</span>
                    <span className="flex-1"/>
                    {t.status === 'failed' && <button onClick={e=>{e.stopPropagation();act(api.retryTask,t.id)}} className="btn-icon" style={{padding:1}}><RotateCcw size={10}/></button>}
                    {['pending','running'].includes(t.status) && <button onClick={e=>{e.stopPropagation();act(api.cancelTask,t.id)}} className="btn-icon" style={{padding:1}}><XCircle size={10}/></button>}
                    {t.status === 'running' && <button onClick={e=>{e.stopPropagation();act(api.holdTask,t.id)}} className="btn-icon" style={{padding:1}}><Square size={10}/></button>}
                    {t.status === 'blocked' && <button onClick={e=>{e.stopPropagation();act(api.releaseTask,t.id)}} className="btn-icon" style={{padding:1}}><Play size={10}/></button>}
                  </div>
                </div>
              ))}
              {tasksByStatus(col.key).length === 0 && (
                <div className="fs-10 text-muted" style={{ textAlign: 'center', padding: 12 }}>拖入任务</div>
              )}
            </div>
          ))}
        </div>
      )}

      {expanded && detail && (
        <div style={{ position: 'fixed', bottom: 20, left: '50%', transform: 'translateX(-50%)', width: 600, maxHeight: 300, overflow: 'auto',
          background: '#1c1c1e', border: '1px solid #2c2c2e', borderRadius: 12, padding: 16, boxShadow: '0 16px 48px rgba(0,0,0,0.6)', zIndex: 100 }}>
          <div className="flex-between" style={{ marginBottom: 8 }}>
            <span className="fw-600 fs-13 mono" style={{ color: '#fff' }}>{detail.id}</span>
            <button onClick={() => {setExpanded(null); setDetail(null)}} className="btn-icon"><XCircle size={14}/></button>
          </div>
          <div className="fs-12 text-muted" style={{ marginBottom: 8, lineHeight: 1.5 }}>{detail.description}</div>
          <div className="fs-10 mono" style={{ color: '#555' }}>
            status={detail.status} type={detail.route_type} role={detail.route_role}
          </div>
          {detail.trace && (
            <div style={{ marginTop: 8 }}>
              <div className="fs-10 text-muted" style={{ marginBottom: 4 }}>执行轨迹:</div>
              {Array.isArray(detail.trace) ? detail.trace.map((tr:any,i:number) => {
                const ts = tr.ts ? new Date(tr.ts).toLocaleTimeString() : ''
                const msg = typeof tr === 'string' ? tr : (tr.msg || tr.message || JSON.stringify(tr).slice(0, 80))
                return <div key={i} className="fs-10 mono" style={{ color: tr.level==='error'?'#f85149':'#666', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>{ts && <span style={{ color: '#444' }}>{ts} </span>}{msg}</div>
              }) : <pre className="fs-10" style={{ color: '#666', whiteSpace: 'pre-wrap', maxHeight: 150, overflow: 'auto' }}>{typeof detail.trace === 'string' ? detail.trace : JSON.stringify(detail.trace, null, 2)}</pre>}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
