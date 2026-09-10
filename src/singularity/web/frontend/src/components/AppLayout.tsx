import { useState, useEffect } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useAppStore } from '../stores/app'
import { useSSE, useSSEConnected } from '../lib/useSSE'
import { useToast } from '../lib/toast'
import { getPinned } from '../lib/pinned'
import { api } from '../lib/api'
import { MessageSquare, List, Settings, Boxes, Activity } from 'lucide-react'

const NAV = [
  { path: '/', label: '对话', icon: MessageSquare },
  { path: '/projects', label: '项目', icon: Boxes },
  { path: '/tasks', label: '任务', icon: List },
  { path: '/usage', label: '用量', icon: Activity },
  { path: '/config', label: '配置', icon: Settings },
]

const PHASE_CN: Record<string,string> = {
  template:'待开始', researching:'调研', gate1:'G1 审核', planning:'架构', gate2:'G2 审核',
  executing:'执行中', integrating:'集成', reviewing:'审查', fixing:'修复', gate3:'G3 审核', delivering:'交付', done:'完成'
}

export default function AppLayout() {
  const { sidebarCollapsed, toggleSidebar } = useAppStore()
  const setActiveProject = useAppStore(s => s.setActiveProject)
  const activePid = useAppStore(s => s.activeProjectId)
  const navigate = useNavigate()
  const location = useLocation()
  const pathname = location.pathname
  const [projects, setProjects] = useState<any[]>([])
  const [pinned, setPinned] = useState<string[]>(getPinned)
  const addToast = useToast()
  const sidebarWidth = sidebarCollapsed ? 0 : 260
  const [loopRunning, setLoopRunning] = useState(false)
  const [conflicts, setConflicts] = useState<any[]>([])

  const sseAlive = useSSEConnected()

  const loadProjects = async () => {
    try { const d: any = await api.projects(); setProjects(Array.isArray(d)?d:(d?.projects||[])) } catch { addToast('加载项目失败', 'error') }
    setPinned(getPinned())   // 置顶在项目页改的，这里跟着重排
  }
  useEffect(() => {
    loadProjects()
    if (sseAlive) return   // SSE 活着 → 靠事件驱动；断了才退回轮询
    const t = setInterval(loadProjects, 10000); return () => clearInterval(t)
  }, [sseAlive])
  useSSE(loadProjects, { kinds: ['project', 'workflow', 'system', 'task'], debounceMs: 400 })

  // 调度状态 / 冲突 没有 SSE 事件，只能轮询；30s 够用
  const refreshLoop = async () => {
    const s = await api.loopStatus().catch(() => null)
    if (s) setLoopRunning(!!(s as any).running)
  }
  useEffect(() => {
    const f = async () => {
      const [s, c] = await Promise.all([
        api.loopStatus().catch(() => null),
        api.conflicts().catch(() => null),
      ])
      if (s) setLoopRunning(!!(s as any).running)
      if (c) setConflicts((c as any).conflicts || [])
    }
    f(); const t = setInterval(f, 30000); return () => clearInterval(t)
  }, [])

  const selectProject = (pid: string) => { setActiveProject(pid); navigate('/') }

  return (
    <div className="app-shell">
      <div className="sidebar" style={{ width: sidebarWidth }}>
        <div style={{ padding: '16px 14px 12px' }}>
          <span style={{ fontSize: 14, fontWeight: 700, color: '#141413', letterSpacing: 1.5 }}>SINGULARITY</span>
        </div>

        <div style={{ padding: '4px 8px', display: 'flex', flexDirection: 'column', gap: 1 }}>
          {NAV.map(n => {
            const active = pathname === n.path
            return (
              <button key={n.path} onClick={() => { if (n.path === '/') setActiveProject('_default'); navigate(n.path) }} className={active ? 'nav-item nav-active' : 'nav-item'}>
                <n.icon size={15}/> {n.label}
              </button>
            )
          })}
        </div>

        <div style={{ padding: '6px 12px 4px', fontSize: 10, color: '#9a9993', fontWeight: 600 }}>项目列表</div>
        <div style={{ flex: 1, overflow: 'auto', padding: '0 6px' }}>
          {[...projects].sort((a, b) => {
              const aPin = pinned.includes(a.id) ? 0 : 1
              const bPin = pinned.includes(b.id) ? 0 : 1
              return aPin - bPin || a.name.localeCompare(b.name)
            }).map((p: any) => {
              const isPinned = pinned.includes(p.id)
              const isActive = activePid === p.id
              return (
                <div key={p.id} onClick={() => selectProject(p.id)} role="button" tabIndex={0}
                  onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectProject(p.id) } }}
                  style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '5px 8px', margin: '1px 0',
                    borderRadius: 6, cursor: 'pointer', fontSize: 12, color: isActive ? '#141413' : '#6b6b68',
                    background: isActive ? '#f3f2ec' : 'transparent' }}>
                  <span style={{ color: isPinned ? '#d97706' : '#b5b2a8', fontSize: 10 }}>#</span>
                  <span className="truncate" style={{ flex: 1 }}>{p.name}</span>
                  <span className="fs-10" style={{ color: '#b5b2a8' }}>{PHASE_CN[p.phase] || p.phase}</span>
                </div>
              )
            })}
        </div>

        <div style={{ padding: '8px 12px', borderTop: '1px solid #f3f2ec', display: 'flex', flexDirection: 'column', gap: 3 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11 }}>
            <span style={{ color: loopRunning ? '#16a34a' : '#b5b2a8', fontSize: 8 }}>●</span>
            <span style={{ color: '#6b6b68' }}>调度{loopRunning ? '运行中' : '已停'}</span>
            {/* 原来只显示状态、**没有任何启停入口**（api.ts 的 startLoop/stopLoop 零调用）——
                侧边栏写着"运行中/已停"却没法操作，只能去敲 CLI。 */}
            <button
              className="btn-icon"
              style={{ fontSize: 11, padding: '0 5px' }}
              title={loopRunning ? '停止调度循环' : '启动调度循环'}
              onClick={async () => {
                try {
                  await (loopRunning ? api.stopLoop() : api.startLoop())
                } catch { /* 失败也无所谓，下面的重拉会显示真实状态 */ }
                setTimeout(refreshLoop, 400)
              }}
            >{loopRunning ? '停' : '启'}</button>
            {conflicts.length > 0 && (
              <span style={{ marginLeft: 'auto', color: '#dc2626' }}
                title={conflicts.map((c: any) => c.task_id || c.id || '').join(', ')}>⚠ {conflicts.length} 冲突</span>
            )}
          </div>
          {/* 用量统计整个搬到「用量」页去了 —— 侧边栏不再重复显示一份。
              留在这里只会挤占项目列表，而且"未配置价格"那种字挤在 260px 里本来就难读。 */}
        </div>
      </div>

      {sidebarCollapsed && (
        <button onClick={toggleSidebar} aria-label="展开侧边栏" style={{ position:'fixed',left:8,top:10,zIndex:10,background:'#f3f2ec',border:'none',borderRadius:6,color:'#9a9993',cursor:'pointer',padding:6 }}>
          <MessageSquare size={14}/>
        </button>
      )}

      <main className="main-area">
        <div className="main-scroll"><Outlet /></div>
      </main>
    </div>
  )
}
