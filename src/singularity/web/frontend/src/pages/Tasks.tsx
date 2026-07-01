import { useState, useEffect, useCallback } from 'react'
import { api, Task } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { Play, Square, RotateCcw, XCircle, Plus, RefreshCw, ChevronDown, ChevronRight, Trash2 } from 'lucide-react'

const COLUMNS = [
  { key: 'pending', label: '待处理', color: 'var(--text-muted)' },
  { key: 'running', label: '进行中', color: 'var(--accent)' },
  { key: 'done', label: '已完成', color: 'var(--accent-green)' },
  { key: 'failed', label: '失败', color: 'var(--accent-red)' },
  { key: 'blocked', label: '已暂停', color: '#f0883e' },
]

export default function Tasks() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [showCreate, setShowCreate] = useState(false)
  const [desc, setDesc] = useState('')
  const [expanded, setExpanded] = useState<string|null>(null)
  const [detail, setDetail] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [dragOver, setDragOver] = useState<string>('')

  const fetch = useCallback(() => {
    setLoading(true)
    api.tasks().then(setTasks).finally(()=>setLoading(false))
  }, [])
  useEffect(() => { fetch() }, [fetch])
  useSSE(() => { fetch() })
  useEffect(() => { const t=setInterval(fetch,10000); return ()=>clearInterval(t) }, [fetch])

  const toggle = async (id: string) => {
    if (expanded === id) { setExpanded(null); setDetail(null); return }
    setExpanded(id)
    try { const d = await api.task(id); setDetail(d) } catch { setDetail(null) }
  }

  const create = () => {
    if (desc.trim()) { api.createTask(desc).then(()=>{setShowCreate(false);setDesc('');fetch()}) }
  }

  const act = (fn: (id:string)=>Promise<any>, id: string) => { fn(id).then(fetch) }

  const handleDragStart = (e: React.DragEvent, tid: string) => {
    e.dataTransfer.setData('taskId', tid)
  }

  const handleDrop = async (e: React.DragEvent, newStatus: string) => {
    e.preventDefault()
    setDragOver('')
    const tid = e.dataTransfer.getData('taskId')
    const task = tasks.find(t => t.id === tid)
    if (!task || task.status === newStatus) return
    // Map status changes to actions
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
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
        <h2 style={{ fontSize: 15, fontWeight: 600 }}>任务</h2>
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{tasks.length} 个</span>
        <span style={{ flex: 1 }} />
        <button onClick={fetch} style={{background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2}}><RefreshCw size={14}/></button>
        <button onClick={()=>setShowCreate(!showCreate)} style={{background:'var(--accent-green)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'5px 10px',cursor:'pointer',fontSize:11,display:'flex',alignItems:'center',gap:4}}><Plus size={12}/> 新建</button>
      </div>

      {showCreate && (
        <div style={{ display: 'flex', gap: 8, marginBottom: 8, padding: 8, background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
          <input value={desc} onChange={e=>setDesc(e.target.value)} placeholder="任务描述..." onKeyDown={e=>e.key==='Enter'&&create()}
            style={{ flex: 1, background: 'var(--bg-tertiary)', border: '1px solid var(--border)', borderRadius: 4, padding: '6px 10px', color: 'var(--text-primary)', fontSize: 12 }} />
          <button onClick={create} style={{ background: 'var(--accent-green)', color: '#fff', border: 'none', borderRadius: 4, padding: '6px 12px', cursor: 'pointer', fontSize: 11 }}>创建</button>
        </div>
      )}

      {/* Kanban 看板 */}
      {loading ? <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>加载中...</div> : (
        <div style={{ flex: 1, display: 'flex', gap: 8, overflow: 'auto' }}>
          {COLUMNS.map(col => (
            <div key={col.key}
              onDragOver={e => { e.preventDefault(); setDragOver(col.key) }}
              onDragLeave={() => setDragOver('')}
              onDrop={e => handleDrop(e, col.key)}
              style={{
                flex: 1, minWidth: 160, background: dragOver === col.key ? 'var(--bg-tertiary)' : 'var(--bg-secondary)',
                borderRadius: 'var(--radius)', padding: '6px 8px', display: 'flex', flexDirection: 'column',
                border: dragOver === col.key ? `1px dashed ${col.color}` : '1px solid transparent',
              }}>
              <div style={{ fontSize: 11, fontWeight: 600, color: col.color, marginBottom: 6, display: 'flex', alignItems: 'center', gap: 4 }}>
                {col.label}
                <span style={{ fontSize: 10, color: 'var(--text-muted)', fontWeight: 400 }}>
                  {tasksByStatus(col.key).length}
                </span>
              </div>
              {tasksByStatus(col.key).map((t: Task) => (
                <div key={t.id} draggable onDragStart={e => handleDragStart(e, t.id)}
                  onClick={() => toggle(t.id)}
                  style={{
                    padding: '6px 8px', marginBottom: 4, borderRadius: 4, cursor: 'grab',
                    background: 'var(--bg-primary)', border: '1px solid var(--border)',
                    borderLeft: `3px solid ${col.color}`, fontSize: 11,
                  }}>
                  <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginBottom: 2 }}>
                    {t.description.length > 60 ? t.description.slice(0, 60) + '...' : t.description}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 9, color: 'var(--text-muted)' }}>
                    <span style={{ fontFamily: 'var(--font-mono)' }}>{t.id.slice(0, 8)}</span>
                    <span style={{ flex: 1 }} />
                    {/* 快捷操作按钮 */}
                    {t.status === 'failed' && <button onClick={e=>{e.stopPropagation();act(api.retryTask,t.id)}} style={{...actBtn}} title="重试"><RotateCcw size={10}/></button>}
                    {['pending','running'].includes(t.status) && <button onClick={e=>{e.stopPropagation();act(api.cancelTask,t.id)}} style={{...actBtn}} title="取消"><XCircle size={10}/></button>}
                    {t.status === 'running' && <button onClick={e=>{e.stopPropagation();act(api.holdTask,t.id)}} style={{...actBtn}} title="暂停"><Square size={10}/></button>}
                    {t.status === 'blocked' && <button onClick={e=>{e.stopPropagation();act(api.releaseTask,t.id)}} style={{...actBtn}} title="释放"><Play size={10}/></button>}
                  </div>
                </div>
              ))}
              {tasksByStatus(col.key).length === 0 && (
                <div style={{ fontSize: 10, color: 'var(--text-muted)', textAlign: 'center', padding: 8, opacity: 0.5 }}>
                  拖拽任务到这里
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 展开的详情 */}
      {expanded && detail && (
        <div style={{ position: 'fixed', bottom: 20, left: '50%', transform: 'translateX(-50%)', width: 600, maxHeight: 300, overflow: 'auto',
          background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: 14, boxShadow: '0 8px 30px rgba(0,0,0,0.4)', zIndex: 100 }}>
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
            <span style={{ fontWeight: 600, fontSize: 13, flex: 1 }}>{detail.id}</span>
            <button onClick={() => {setExpanded(null); setDetail(null)}} style={{ background: 'none', border: 'none', color: 'var(--text-secondary)', cursor: 'pointer' }}><XCircle size={14}/></button>
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 4 }}>{detail.description}</div>
          <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
            status={detail.status} type={detail.route_type} role={detail.route_role}
          </div>
          {detail.trace && <pre style={{ fontSize: 10, color: 'var(--text-muted)', whiteSpace: 'pre-wrap', maxHeight: 150, overflow: 'auto', marginTop: 6 }}>{JSON.stringify(detail.trace, null, 2)}</pre>}
        </div>
      )}
    </div>
  )
}

const actBtn: React.CSSProperties = { background: 'none', border: 'none', color: 'var(--text-secondary)', cursor: 'pointer', padding: 1 }
