import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { api, TaskDetail as TD } from '../lib/api'
import { ArrowLeft, RotateCcw, XCircle, CheckCircle, Play, RefreshCw } from 'lucide-react'

export default function TaskDetail() {
  const { id } = useParams<{id:string}>()
  const nav = useNavigate()
  const [task, setTask] = useState<TD | null>(null)
  const [trace, setTrace] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!id) return
    Promise.all([api.task(id), api.taskTrace(id)]).then(([t, tr]) => {
      setTask(t); setTrace(tr); setLoading(false)
    }).catch(() => setLoading(false))
  }, [id])

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>
  if (!task) return <div style={{color:'var(--accent-red)'}}>任务不存在</div>

  return (
    <div>
      <button onClick={()=>nav('/tasks')} style={{background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',display:'flex',alignItems:'center',gap:4,fontSize:12,marginBottom:10}}>
        <ArrowLeft size={14}/> 返回任务列表</button>

      <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:16,marginBottom:12}}>
        <div style={{fontSize:16,fontWeight:600,marginBottom:8}}>{task.description}</div>
        <div style={{display:'flex',gap:10,fontSize:12,color:'var(--text-secondary)',fontFamily:'var(--font-mono)'}}>
          <span>ID: {task.id}</span><span>层级: {task.route_level}</span><span>状态: {task.status}</span>
          {task.route_role && <span>角色: {task.route_role}</span>}
          {task.project_id && <span>项目: {task.project_id.slice(0,8)}</span>}
        </div>
      </div>

      {/* Actions */}
      <div style={{display:'flex',gap:6,marginBottom:12}}>
        {task.status === 'failed' && <button onClick={()=>api.retryTask(task.id).then(()=>location.reload())} style={btnStyle('var(--accent)')}><RotateCcw size={14}/> 重试</button>}
        {['pending','running'].includes(task.status) && <button onClick={()=>api.cancelTask(task.id).then(()=>location.reload())} style={btnStyle('var(--accent-red)')}><XCircle size={14}/> 取消</button>}
        {task.status === 'running' && <button onClick={()=>api.holdTask(task.id).then(()=>location.reload())} style={btnStyle('var(--accent-yellow)')}>暂停</button>}
        <button onClick={()=>api.deleteTask(task.id).then(()=>nav('/tasks'))} style={btnStyle('var(--text-muted)')}>删除</button>
      </div>

      {/* Trace */}
      {trace && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
          <div style={{fontSize:13,fontWeight:600,marginBottom:8}}>执行 Trace</div>
          <pre style={{fontSize:11,fontFamily:'var(--font-mono)',color:'var(--text-secondary)',whiteSpace:'pre-wrap',maxHeight:400,overflow:'auto'}}>
            {JSON.stringify(trace, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
}

const btnStyle = (color: string) => ({
  background: color+'22', color, border: `1px solid ${color}44`, borderRadius: 'var(--radius)',
  padding: '6px 12px', cursor: 'pointer', fontSize: 12, display: 'flex', alignItems: 'center', gap: 4,
})
