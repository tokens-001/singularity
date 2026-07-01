import { useState, useEffect, useCallback } from 'react'
import { api, Task } from '../lib/api'
import { useSSE } from '../lib/useSSE'
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
      {/* 顶栏 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <h2 style={{ fontSize: 15, fontWeight: 600, color: '#fff' }}>任务</h2>
        <span style={{ fontSize: 11, color: '#666' }}>{tasks.length} 个</span>
        <span style={{ flex: 1 }} />
        <button onClick={fetch} style={{ ...iconBtn }}><RefreshCw size={14}/></button>
        <button onClick={()=>setShowCreate(!showCreate)}
          style={{ background:'#fff',color:'#000',border:'none',borderRadius:6,padding:'5px 12px',cursor:'pointer',fontSize:11,fontWeight:600,display:'flex',alignItems:'center',gap:4 }}>
          <Plus size={12}/> 新建
        </button>
      </div>

      {showCreate && (
        <div style={{ display: 'flex', gap: 8, marginBottom: 10, padding: 8, background: '#1c1c1e', borderRadius: 8 }}>
          <input value={desc} onChange={e=>setDesc(e.target.value)} placeholder="任务描述..." onKeyDown={e=>e.key==='Enter'&&create()}
            style={{ flex: 1, background: '#000', border: '1px solid #2c2c2e', borderRadius: 6, padding: '6px 10px', color: '#fff', fontSize: 12, outline: 'none' }} />
          <button onClick={create} style={{ background: '#fff', color: '#000', border: 'none', borderRadius: 6, padding: '6px 14px', cursor: 'pointer', fontSize: 11, fontWeight: 600 }}>创建</button>
        </div>
      )}

      {/* Kanban */}
      {loading ? <div style={{ color: '#666', fontSize: 12 }}>加载中...</div> : (
        <div style={{ flex: 1, display: 'flex', gap: 10, overflow: 'auto' }}>
          {COLUMNS.map(col => (
            <div key={col.key}
              onDragOver={e => { e.preventDefault(); setDragOver(col.key) }}
              onDragLeave={() => setDragOver('')}
              onDrop={e => handleDrop(e, col.key)}
              style={{
                flex: 1, minWidth: 170, background: dragOver === col.key ? '#1c1c1e' : '#0a0a0a',
                borderRadius: 10, padding: '8px 10px', display: 'flex', flexDirection: 'column',
                border: dragOver === col.key ? `1px dashed ${col.color}` : '1px solid #1c1c1e',
              }}>
              <div style={{ fontSize: 11, fontWeight: 600, color: col.color, marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ width: 6, height: 6, borderRadius: 3, background: col.color, display: 'inline-block' }}/>
                {col.label}
                <span style={{ fontSize: 10, color: '#555', fontWeight: 400 }}>
                  {tasksByStatus(col.key).length}
                </span>
              </div>
              {tasksByStatus(col.key).map((t: Task) => (
                <div key={t.id} draggable onDragStart={e => handleDragStart(e, t.id)}
                  onClick={() => toggle(t.id)}
                  style={{
                    padding: '8px 10px', marginBottom: 6, borderRadius: 8, cursor: 'grab',
                    background: '#1c1c1e', border: '1px solid #2c2c2e',
                    borderLeft: `3px solid ${col.color}`, fontSize: 11, color: '#ccc',
                  }}>
                  <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginBottom: 4, lineHeight: 1.4 }}>
                    {t.description.split('\n')[0].slice(0, 80)}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 9, color: '#555' }}>
                    <span style={{ fontFamily: 'monospace' }}>{t.id.slice(0, 8)}</span>
                    <span style={{ flex: 1 }} />
                    {t.status === 'failed' && <button onClick={e=>{e.stopPropagation();act(api.retryTask,t.id)}} style={actBtn}><RotateCcw size={10}/></button>}
                    {['pending','running'].includes(t.status) && <button onClick={e=>{e.stopPropagation();act(api.cancelTask,t.id)}} style={actBtn}><XCircle size={10}/></button>}
                    {t.status === 'running' && <button onClick={e=>{e.stopPropagation();act(api.holdTask,t.id)}} style={actBtn}><Square size={10}/></button>}
                    {t.status === 'blocked' && <button onClick={e=>{e.stopPropagation();act(api.releaseTask,t.id)}} style={actBtn}><Play size={10}/></button>}
                  </div>
                </div>
              ))}
              {tasksByStatus(col.key).length === 0 && (
                <div style={{ fontSize: 10, color: '#444', textAlign: 'center', padding: 12 }}>拖入任务</div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 详情浮层 */}
      {expanded && detail && (
        <div style={{ position: 'fixed', bottom: 20, left: '50%', transform: 'translateX(-50%)', width: 600, maxHeight: 300, overflow: 'auto',
          background: '#1c1c1e', border: '1px solid #2c2c2e', borderRadius: 12, padding: 16, boxShadow: '0 16px 48px rgba(0,0,0,0.6)', zIndex: 100 }}>
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
            <span style={{ fontWeight: 600, fontSize: 13, color: '#fff', flex: 1, fontFamily: 'monospace' }}>{detail.id}</span>
            <button onClick={() => {setExpanded(null); setDetail(null)}} style={{ background: 'none', border: 'none', color: '#666', cursor: 'pointer' }}><XCircle size={14}/></button>
          </div>
          <div style={{ fontSize: 12, color: '#999', marginBottom: 8, lineHeight: 1.5 }}>{detail.description}</div>
          <div style={{ fontSize: 10, color: '#555', fontFamily: 'monospace' }}>
            status={detail.status} type={detail.route_type} role={detail.route_role}
          </div>
          {detail.trace && <pre style={{ fontSize: 10, color: '#666', whiteSpace: 'pre-wrap', maxHeight: 150, overflow: 'auto', marginTop: 8 }}>{JSON.stringify(detail.trace, null, 2)}</pre>}
        </div>
      )}
    </div>
  )
}

const iconBtn: React.CSSProperties = { background:'none',border:'none',color:'#666',cursor:'pointer',padding:4 }
const actBtn: React.CSSProperties = { background:'none',border:'none',color:'#555',cursor:'pointer',padding:1 }
