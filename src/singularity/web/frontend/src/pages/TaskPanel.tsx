import { useState, useEffect } from 'react'
import { useAppStore, Task } from '../stores/app'
import { Play, Square, RotateCcw, XCircle, Search, Filter, ChevronRight } from 'lucide-react'

const STATUS_COLORS: Record<string, string> = {
  pending: 'var(--text-muted)',
  running: 'var(--accent)',
  done: 'var(--accent-green)',
  failed: 'var(--accent-red)',
  cancelled: 'var(--accent-yellow)',
}

const LEVEL_TAGS: Record<string, string> = { E: '#58a6ff', 'E+': '#a371f7', D: '#f0883e' }

// Mock data for demo
const MOCK_TASKS: Task[] = [
  { id: 'T1', title: '实现 OAuth2 登录模块', status: 'done', level: 'E', agent: 'kimi-k2.7', updated_at: Date.now()-3600 },
  { id: 'T2', title: '代码片段 CRUD API', status: 'running', level: 'E+', agent: 'qwen3-coder', updated_at: Date.now()-600 },
  { id: 'T3', title: 'PostgreSQL 全文搜索索引', status: 'running', level: 'E', agent: 'kimi-k2.7', updated_at: Date.now()-300 },
  { id: 'T4', title: '团队 RBAC 权限系统', status: 'pending', level: 'D', agent: 'gpt-5.5', updated_at: Date.now()-120 },
  { id: 'T5', title: 'Docker Compose 部署配置', status: 'failed', level: 'E', agent: 'qwen3-coder', updated_at: Date.now()-60 },
  { id: 'T6', title: '前端组件库搭建', status: 'pending', level: 'E+', agent: 'glm-5.2', updated_at: Date.now() },
]

export default function TaskPanel() {
  const { tasks: storeTasks, setTasks, taskFilters, setTaskFilters } = useAppStore()
  const tasks = storeTasks.length ? storeTasks : MOCK_TASKS

  const filtered = tasks.filter(t => {
    if (taskFilters.status && t.status !== taskFilters.status) return false
    if (taskFilters.level && t.level !== taskFilters.level) return false
    return true
  })

  const counts = { total: tasks.length, running: tasks.filter(t=>t.status==='running').length,
    done: tasks.filter(t=>t.status==='done').length, failed: tasks.filter(t=>t.status==='failed').length }

  return (
    <div>
      {/* Stats bar */}
      <div style={{ display: 'flex', gap: 16, marginBottom: 16 }}>
        {[{label:'全部',v:counts.total,color:'var(--text-primary)'},{label:'运行中',v:counts.running,color:'var(--accent)'},{label:'完成',v:counts.done,color:'var(--accent-green)'},{label:'失败',v:counts.failed,color:'var(--accent-red)'}].map(s => (
          <div key={s.label} style={{ background:'var(--bg-secondary)', border:'1px solid var(--border)', borderRadius:'var(--radius)', padding:'10px 16px', minWidth:100 }}>
            <div style={{ fontSize: 22, fontWeight: 700, color: s.color }}>{s.v}</div>
            <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{s.label}</div>
          </div>
        ))}
      </div>

      {/* Filters */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
        <select value={taskFilters.status} onChange={e=>setTaskFilters({status:e.target.value})}
          style={{ background:'var(--bg-tertiary)', color:'var(--text-primary)', border:'1px solid var(--border)', borderRadius:'var(--radius)', padding:'6px 10px', fontSize:12 }}>
          <option value="">全部状态</option>
          {['pending','running','done','failed','cancelled'].map(s=><option key={s} value={s}>{s}</option>)}
        </select>
        <select value={taskFilters.level} onChange={e=>setTaskFilters({level:e.target.value})}
          style={{ background:'var(--bg-tertiary)', color:'var(--text-primary)', border:'1px solid var(--border)', borderRadius:'var(--radius)', padding:'6px 10px', fontSize:12 }}>
          <option value="">全部层级</option>
          {['E','E+','D'].map(l=><option key={l} value={l}>{l}</option>)}
        </select>
      </div>

      {/* Task table */}
      <div style={{ background:'var(--bg-secondary)', border:'1px solid var(--border)', borderRadius:'var(--radius)', overflow:'hidden' }}>
        <table style={{ width:'100%', borderCollapse:'collapse' }}>
          <thead>
            <tr style={{ borderBottom:'1px solid var(--border)', fontSize:11, color:'var(--text-muted)', textAlign:'left' }}>
              <th style={{ padding:'8px 12px', width:80 }}>ID</th>
              <th style={{ padding:'8px 12px' }}>任务</th>
              <th style={{ padding:'8px 12px', width:80 }}>层级</th>
              <th style={{ padding:'8px 12px', width:100 }}>状态</th>
              <th style={{ padding:'8px 12px', width:120 }}>Agent</th>
              <th style={{ padding:'8px 12px', width:60 }}></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map(t => (
              <tr key={t.id} style={{ borderBottom:'1px solid var(--border)', fontSize:13, cursor:'pointer' }}
                onMouseEnter={e=>{e.currentTarget.style.background='var(--bg-tertiary)'}}
                onMouseLeave={e=>{e.currentTarget.style.background='transparent'}}>
                <td style={{ padding:'8px 12px', fontFamily:'var(--font-mono)', fontSize:11, color:'var(--text-muted)' }}>{t.id}</td>
                <td style={{ padding:'8px 12px' }}>{t.title}</td>
                <td style={{ padding:'8px 12px' }}>
                  <span style={{ background: LEVEL_TAGS[t.level]+'22', color: LEVEL_TAGS[t.level], padding:'1px 6px', borderRadius:3, fontSize:11, fontWeight:600 }}>{t.level}</span>
                </td>
                <td style={{ padding:'8px 12px' }}>
                  <span style={{ display:'flex', alignItems:'center', gap:4, color:STATUS_COLORS[t.status]||'var(--text-secondary)' }}>
                    <span style={{ width:6, height:6, borderRadius:'50%', background:STATUS_COLORS[t.status]||'var(--text-secondary)', display:'inline-block' }} />
                    {t.status}
                  </span>
                </td>
                <td style={{ padding:'8px 12px', fontFamily:'var(--font-mono)', fontSize:11, color:'var(--text-secondary)' }}>{t.agent}</td>
                <td style={{ padding:'8px 12px' }}><ChevronRight size={14} color='var(--text-muted)' /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
