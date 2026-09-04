import { useState, useEffect, useCallback } from 'react'
import { api, Task } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { useToast } from '../components/Toast'
import { Plus, RefreshCw, RotateCcw, XCircle, Trash2, Search } from 'lucide-react'

const STATUS_CN: Record<string,string> = { pending:'待处理', running:'进行中', done:'已完成', failed:'失败', blocked:'已暂停', paused:'已暂停' }
const STATUS_COLOR: Record<string,string> = { pending:'#9a9993', running:'#2563eb', done:'#16a34a', failed:'#dc2626', blocked:'#b45309', paused:'#b45309' }

export default function Tasks() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [showCreate, setShowCreate] = useState(false)
  const [desc, setDesc] = useState('')
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const toast = useToast(s => s.add)

  const fetch = useCallback(() => {
    setLoading(true)
    api.tasks().then(setTasks).catch(() => toast('加载任务失败', 'error')).finally(() => setLoading(false))
  }, [])
  useEffect(() => { fetch() }, [fetch])
  useSSE(() => { fetch() })
  useEffect(() => { const t = setInterval(fetch, 10000); return () => clearInterval(t) }, [fetch])

  const create = () => { if (desc.trim()) { api.createTask(desc).then(() => { setShowCreate(false); setDesc(''); fetch() }) } }
  const act = (fn: (id: string) => Promise<any>, id: string) => { fn(id).then(fetch).catch(() => toast('操作失败', 'error')) }

  const list = tasks.filter(t => !search || t.description.toLowerCase().includes(search.toLowerCase()))

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div className="flex-center gap-8" style={{ marginBottom: 12 }}>
        <h2 className="fs-13 fw-600" style={{ color: '#141413' }}>任务</h2>
        <span className="fs-11 text-muted">{tasks.length} 个</span>
        <div className="search-box">
          <Search size={12} color="#9a9993"/>
          <input value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索..." aria-label="搜索任务" className="search-input"/>
        </div>
        <span className="flex-1"/>
        <button onClick={fetch} className="btn-icon"><RefreshCw size={14}/></button>
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
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {list.map(t => (
            <div key={t.id} className="flex-center gap-8" title={t.description}
              style={{ padding: '8px 10px', background: '#fff', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12 }}>
              <span className="status-dot" style={{ background: STATUS_COLOR[t.status] || '#9a9993', flexShrink: 0 }}/>
              <span className="truncate" style={{ flex: 1, color: '#141413' }}>{t.description.split('\n')[0]}</span>
              <span className="fs-10" style={{ color: STATUS_COLOR[t.status] || '#9a9993', flexShrink: 0 }}>{STATUS_CN[t.status] || t.status}</span>
              <span className="fs-10 text-muted mono" style={{ flexShrink: 0 }}>{t.id.slice(0, 8)}</span>
              <span className="flex-center gap-4">
                {t.status === 'failed' && <button onClick={() => act(api.retryTask, t.id)} className="btn-icon" title="重试"><RotateCcw size={12}/></button>}
                {['pending','running','paused'].includes(t.status) && <button onClick={() => act(api.cancelTask, t.id)} className="btn-icon" title="取消"><XCircle size={12}/></button>}
                <button onClick={() => act(api.deleteTask, t.id)} className="btn-icon" title="删除" style={{ color: '#dc2626' }}><Trash2 size={12}/></button>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
