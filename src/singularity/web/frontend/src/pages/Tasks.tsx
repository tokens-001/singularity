import { useState, useEffect, useCallback } from 'react'
import { api, Task } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { Play, Square, RotateCcw, XCircle, Plus, RefreshCw, ChevronDown, ChevronRight } from 'lucide-react'

const STATUS_CN: Record<string,string> = { pending:'待处理', running:'运行中', done:'已完成', failed:'失败', cancelled:'已取消', blocked:'已暂停' }
const SC: Record<string,string> = { pending:'var(--text-muted)', running:'var(--accent)', done:'var(--accent-green)', failed:'var(--accent-red)', cancelled:'var(--accent-yellow)', blocked:'#f0883e' }

export default function Tasks() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [filter, setFilter] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [desc, setDesc] = useState('')
  const [expanded, setExpanded] = useState<string|null>(null)
  const [detail, setDetail] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  const fetch = useCallback(() => {
    setLoading(true)
    api.tasks(filter?`?status=${filter}`:'').then(setTasks).finally(()=>setLoading(false))
  }, [filter])
  useEffect(() => { fetch() }, [fetch])
  useSSE(() => { fetch() })
  useEffect(() => { const t=setInterval(fetch,15000); return ()=>clearInterval(t) }, [fetch])

  const toggle = async (id: string) => {
    if (expanded === id) { setExpanded(null); setDetail(null); return }
    setExpanded(id)
    try { const d = await api.task(id); setDetail(d) } catch { setDetail(null) }
  }

  const create = () => { if(desc.trim()){ api.createTask(desc).then(()=>{setShowCreate(false);setDesc('');fetch()}) } }
  const filtered = filter ? tasks.filter((t: Task) => t.status === filter) : tasks

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600 }}>任务</h2>
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{tasks.length} 个</span>
        <div style={{ display: 'flex', gap: 4, marginLeft: 8 }}>
          {['','pending','running','done','failed'].map(s=>(
            <button key={s} onClick={()=>setFilter(s)} style={filter===s?fActive:fInactive}>{s||'全部'}</button>
          ))}
        </div>
        <span style={{ flex: 1 }} />
        <button onClick={fetch} style={{background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2}}><RefreshCw size={14}/></button>
        <button onClick={()=>setShowCreate(!showCreate)} style={{background:'var(--accent-green)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'6px 12px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:4}}><Plus size={14}/> 新建</button>
      </div>

      {showCreate && (
        <div style={{ display: 'flex', gap: 8, marginBottom: 10, padding: 10, background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
          <input value={desc} onChange={e=>setDesc(e.target.value)} placeholder="任务描述..." onKeyDown={e=>e.key==='Enter'&&create()}
            style={{ flex: 1, background: 'var(--bg-tertiary)', border: '1px solid var(--border)', borderRadius: 4, padding: '6px 10px', color: 'var(--text-primary)', fontSize: 13 }} />
          <button onClick={create} style={{ background: 'var(--accent-green)', color: '#fff', border: 'none', borderRadius: 4, padding: '6px 14px', cursor: 'pointer', fontSize: 12 }}>创建</button>
        </div>
      )}

      {loading && <div style={{ color: 'var(--text-muted)', fontSize: 12, padding: 20 }}>加载中...</div>}
      {!loading && filtered.length === 0 && <div style={{ color: 'var(--text-muted)', fontSize: 12, padding: 20 }}>无任务</div>}
      {!loading && filtered.map((t: Task) => (
        <div key={t.id} style={{ marginBottom: 4 }}>
          <div onClick={()=>toggle(t.id)}
            style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', cursor: 'pointer', borderLeft: `3px solid ${SC[t.status]||'var(--border)'}` }}>
            <span style={{ color: 'var(--text-muted)' }}>{expanded===t.id?<ChevronDown size={12}/>:<ChevronRight size={12}/>}</span>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)', minWidth: 60 }}>{t.id.slice(0,8)}</span>
            <span style={{ flex: 1, fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.description}</span>
            <span style={{ fontSize: 10, color: SC[t.status]||'var(--text-muted)', fontWeight: 600 }}>{STATUS_CN[t.status]||t.status}</span>
            <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{new Date(t.created_at*1000).toLocaleTimeString('zh-CN')}</span>
            <ActionBtns t={t} onDone={fetch} />
          </div>
          {expanded === t.id && detail && (
            <div style={{ padding: '8px 14px', marginLeft: 20, borderLeft: '1px solid var(--border)', fontSize: 12, color: 'var(--text-secondary)' }}>
              <div><b>ID:</b> {detail.id} &nbsp; <b>类型:</b> {detail.route_type||'-'}</div>
              <div style={{ marginTop: 4 }}><b>描述:</b> {detail.description}</div>
              {detail.trace && <div style={{ marginTop: 4, fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)', whiteSpace: 'pre-wrap', maxHeight: 200, overflow: 'auto' }}>{JSON.stringify(detail.trace, null, 2)}</div>}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function ActionBtns({ t, onDone }: { t: Task; onDone: () => void }) {
  const act = (fn: (id:string)=>Promise<any>) => { fn(t.id).then(onDone) }
  return (
    <div onClick={e=>e.stopPropagation()} style={{ display: 'flex', gap: 2 }}>
      {t.status==='failed' && <button onClick={()=>act(api.retryTask)} style={{background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2}} title="重试"><RotateCcw size={12}/></button>}
      {['pending','running'].includes(t.status) && <button onClick={()=>act(api.cancelTask)} style={{background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2}} title="取消"><XCircle size={12}/></button>}
      {t.status==='running' && <button onClick={()=>act(api.holdTask)} style={{background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2}} title="暂停"><Square size={12}/></button>}
      {t.status==='blocked' && <button onClick={()=>act(api.releaseTask)} style={{background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2}} title="释放"><Play size={12}/></button>}
    </div>
  )
}

const fActive: React.CSSProperties = { background: 'var(--accent)', color: '#fff', border: '1px solid var(--accent)', borderRadius: 4, padding: '2px 8px', cursor: 'pointer', fontSize: 11 }
const fInactive: React.CSSProperties = { background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', border: '1px solid var(--border)', borderRadius: 4, padding: '2px 8px', cursor: 'pointer', fontSize: 11 }
