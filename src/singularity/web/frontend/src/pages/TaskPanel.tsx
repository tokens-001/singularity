import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, Task } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { Play, Square, RotateCcw, XCircle, Plus, RefreshCw, ChevronRight, CheckCircle, Pause } from 'lucide-react'

const STATUS_CN: Record<string, string> = {
  pending: '待处理', running: '运行中', done: '已完成', failed: '失败', cancelled: '已取消', blocked: '已暂停', needs_approval: '待审批',
}
const STATUS_COLORS: Record<string, string> = {
  pending: 'var(--text-muted)', running: 'var(--accent)',
  done: 'var(--accent-green)', failed: 'var(--accent-red)', cancelled: 'var(--accent-yellow)',
  blocked: '#f0883e', needs_approval: 'var(--accent-purple)',
}

export default function TaskPanel() {
  const nav = useNavigate()
  const [tasks, setTasks] = useState<Task[]>([])
  const [statusFilter, setStatusFilter] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [newDesc, setNewDesc] = useState('')
  const [loading, setLoading] = useState(true)

  const fetchTasks = useCallback(() => {
    setLoading(true)
    const params = new URLSearchParams()
    if (statusFilter) params.set('status', statusFilter)
    const qs = params.toString()
    api.tasks(qs ? `?${qs}` : '').then(setTasks).finally(() => setLoading(false))
  }, [statusFilter])

  useEffect(() => { fetchTasks() }, [fetchTasks])

  // SSE 实时更新: 任务状态变化时立即刷新
  useSSE((event) => {
    if (event.kind === 'init' || event.kind === 'task_start' || event.kind === 'task_done' ||
        event.kind === 'task_fail' || event.kind === 'task_cancel' || event.kind === 'system') {
      fetchTasks()
    }
  })

  // 兜底: 每 30s 轮询一次 (SSE 中断时也能更新)
  useEffect(() => {
    const t = setInterval(fetchTasks, 30000)
    return () => clearInterval(t)
  }, [fetchTasks])

  const filtered = tasks
  const counts = { total: tasks.length, running: tasks.filter(t=>t.status==='running').length,
    done: tasks.filter(t=>t.status==='done').length, failed: tasks.filter(t=>t.status==='failed').length }

  const actions = [
    { icon: RotateCcw, label: '重试', fn: (t:Task) => api.retryTask(t.id).then(fetchTasks), show: (t:Task) => t.status === 'failed' },
    { icon: XCircle, label: '取消', fn: (t:Task) => api.cancelTask(t.id).then(fetchTasks), show: (t:Task) => ['pending','running'].includes(t.status) },
    { icon: Pause, label: '暂停', fn: (t:Task) => api.holdTask(t.id).then(fetchTasks), show: (t:Task) => t.status === 'running' },
    { icon: Play, label: '释放', fn: (t:Task) => api.releaseTask(t.id).then(fetchTasks), show: (t:Task) => t.status === 'blocked' },
    { icon: CheckCircle, label: '应用', fn: (t:Task) => api.applyTask(t.id).then(fetchTasks), show: (t:Task) => t.status === 'needs_approval' || t.route_gate === 'needs_approval' },
  ]

  const handleCreate = () => {
    if (!newDesc.trim()) return
    api.createTask(newDesc).then(() => { setShowCreate(false); setNewDesc(''); fetchTasks() })
  }

  return (
    <div>
      {/* Stats */}
      <div style={{display:'flex',gap:14,marginBottom:14}}>
        {[{l:'全部',v:counts.total,c:'var(--text-primary)'},{l:'运行中',v:counts.running,c:'var(--accent)'},{l:'完成',v:counts.done,c:'var(--accent-green)'},{l:'失败',v:counts.failed,c:'var(--accent-red)'}].map(s=>(
          <div key={s.l} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:'10px 14px',minWidth:90}}>
            <div style={{fontSize:22,fontWeight:700,color:s.c}}>{s.v}</div><div style={{fontSize:11,color:'var(--text-secondary)'}}>{s.l}</div>
          </div>
        ))}
        <button onClick={()=>setShowCreate(!showCreate)} style={{marginLeft:'auto',background:'var(--accent)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'8px 14px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:4,alignSelf:'center'}}>
          <Plus size={14}/> 创建任务</button>
      </div>

      {/* Create form */}
      {showCreate && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14,marginBottom:14,display:'flex',gap:8,alignItems:'center'}}>
          <input value={newDesc} onChange={e=>setNewDesc(e.target.value)} placeholder="任务描述..." style={{flex:1,background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:4,padding:'8px 10px',color:'var(--text-primary)',fontSize:13}} onKeyDown={e=>e.key==='Enter'&&handleCreate()}/>
          <button onClick={handleCreate} style={{background:'var(--accent-green)',color:'#fff',border:'none',borderRadius:4,padding:'8px 14px',cursor:'pointer',fontSize:12}}>创建</button>
        </div>
      )}

      {/* Filters */}
      <div style={{display:'flex',gap:8,marginBottom:10}}>
        <button onClick={fetchTasks} style={{background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:4,padding:'4px 8px',cursor:'pointer',color:'var(--text-secondary)'}}><RefreshCw size={14}/></button>
        {[{v:'',l:'全部'},{v:'pending',l:'待处理'},{v:'running',l:'运行中'},{v:'done',l:'已完成'},{v:'failed',l:'失败'},{v:'cancelled',l:'已取消'},{v:'blocked',l:'已暂停'}].map(({v,l})=>(
          <button key={v} onClick={()=>setStatusFilter(v)} style={{background:statusFilter===v?"var(--accent)":"var(--bg-tertiary)",color:statusFilter===v?"#fff":"var(--text-secondary)",border:"1px solid var(--border)",borderRadius:4,padding:"3px 8px",cursor:"pointer",fontSize:11}}>{l}</button>
        ))}
      </div>

      {/* Table */}
      <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',overflow:'hidden'}}>
        {loading && <div style={{padding:20,color:'var(--text-muted)',fontSize:12}}>加载中...</div>}
        {!loading && filtered.length === 0 && <div style={{padding:20,color:'var(--text-muted)',fontSize:12}}>无任务</div>}
        {!loading && filtered.length > 0 && (
          <table style={{width:'100%',borderCollapse:'collapse'}}>
            <thead><tr style={{borderBottom:'1px solid var(--border)',fontSize:11,color:'var(--text-muted)',textAlign:'left'}}>
              <th style={{padding:"7px 10px"}}>描述</th><th style={{padding:'7px 10px',width:80}}>状态</th><th style={{padding:'7px 10px',width:100}}>操作</th>
            </tr></thead>
            <tbody>
              {filtered.slice(0, 50).map(t=>(
                <tr key={t.id} style={{borderBottom:'1px solid var(--border)',fontSize:12,cursor:'pointer',transition:'background 0.1s'}}
                  onMouseEnter={e=>{e.currentTarget.style.background='var(--bg-tertiary)'}} onMouseLeave={e=>{e.currentTarget.style.background='transparent'}} onClick={()=>nav(`/tasks/${t.id}`)}>
                  <td style={{padding:'7px 10px',fontFamily:'var(--font-mono)',fontSize:10,color:'var(--text-muted)'}}>{t.id.slice(0,8)}</td>
                  <td style={{padding:'7px 10px',maxWidth:400,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{t.description}</td>
                  <td style={{padding:'7px 10px'}}><span style={{display:'flex',alignItems:'center',gap:4,color:STATUS_COLORS[t.status]||'var(--text-secondary)'}}><span style={{width:6,height:6,borderRadius:'50%',background:STATUS_COLORS[t.status]||'var(--text-secondary)',display:'inline-block'}}/>{STATUS_CN[t.status]||t.status}</span></td>
                  <td style={{padding:'7px 10px'}} onClick={e=>e.stopPropagation()}>
                    <div style={{display:'flex',gap:2}}>
                      {actions.filter(a=>a.show(t)).map(a=>(<button key={a.label} onClick={()=>a.fn(t)} title={a.label} style={{background:'transparent',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2}}><a.icon size={14}/></button>))}
                      <button onClick={()=>nav(`/tasks/${t.id}`)} style={{background:'transparent',border:'none',color:'var(--text-muted)',cursor:'pointer',padding:2}}><ChevronRight size={14}/></button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
