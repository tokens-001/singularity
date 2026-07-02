import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { useToast } from '../components/Toast'
import { Activity, Zap, Clock, BarChart3 } from 'lucide-react'

export default function StatusPanel() {
  const [status, setStatus] = useState<any>({})
  const [tokenUsage, setTokenUsage] = useState<any>({})
  const [projects, setProjects] = useState<any[]>([])

  const fetch = async () => {
    try {
      const [s, t, p] = await Promise.all([
        api.status(), api.tokenUsage(),
        api.projects().then((d: any) => Array.isArray(d) ? d : (d?.projects||[])),
      ])
      setStatus(s); setTokenUsage(t); setProjects(p)
    } catch { useToast.getState().add('加载系统状态失败', 'error') }
  }

  useEffect(() => { fetch() }, [])
  useSSE(() => { fetch() })

  const counts = status?.counts || {}
  const projectStats = { done: projects.filter(p => p.phase === 'done').length, active: projects.filter(p => !['done','template'].includes(p.phase||'')).length }

  return (
    <div style={{ padding: '12px 14px', fontSize: 12, color: 'var(--text-secondary)' }}>
      <div style={{ fontWeight: 600, fontSize: 11, color: 'var(--text-muted)', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
        <Activity size={12}/> 系统状态
      </div>

      {/* 任务统计 */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 12 }}>
        {[
          { label: '运行中', val: counts.running||0, color: 'var(--accent)' },
          { label: '已完成', val: counts.done||0, color: 'var(--accent-green)' },
          { label: '失败', val: counts.failed||0, color: 'var(--accent-red)' },
          { label: '待处理', val: counts.pending||0, color: 'var(--text-muted)' },
        ].map(s => (
          <div key={s.label} style={{ background: 'var(--bg-primary)', borderRadius: 4, padding: '6px 8px' }}>
            <div style={{ fontSize: 18, fontWeight: 700, color: s.color }}>{s.val}</div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>{s.label}</div>
          </div>
        ))}
      </div>

      {/* 项目统计 */}
      <div style={{ marginBottom: 12 }}>
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 4 }}>项目</div>
        <div style={{ display: 'flex', gap: 12 }}>
          <span style={{ color: 'var(--accent)' }}>{projectStats.active} 活跃</span>
          <span style={{ color: 'var(--accent-green)' }}>{projectStats.done} 完成</span>
        </div>
      </div>

      {/* Token */}
      <div style={{ marginBottom: 12 }}>
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 4 }}>
          <Zap size={10} style={{display:'inline'}}/> Token 用量
        </div>
        <div style={{ fontSize: 11 }}>
          <span style={{ fontWeight: 600 }}>{((tokenUsage?.total_tokens||0)/1000).toFixed(1)}k</span>
          <span style={{ color: 'var(--text-muted)', marginLeft: 4 }}>
            / {tokenUsage?.budget_total ? ((tokenUsage.budget_total/1000).toFixed(0)+'k') : '∞'}
          </span>
        </div>
        {tokenUsage?.budget_total > 0 && (
          <div style={{ height: 3, background: 'var(--bg-primary)', borderRadius: 2, marginTop: 4 }}>
            <div style={{ height: '100%', width: `${Math.min(100, (tokenUsage.total_tokens||0)/tokenUsage.budget_total*100)}%`,
              background: 'var(--accent)', borderRadius: 2, transition: 'width 0.3s' }}/>
          </div>
        )}
      </div>

      {/* 时间 */}
      <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>
        <Clock size={10} style={{display:'inline'}}/> {status?.uptime ? `${Math.floor(status.uptime/3600)}h ${Math.floor(status.uptime%3600/60)}m` : '刚启动'}
      </div>
    </div>
  )
}
