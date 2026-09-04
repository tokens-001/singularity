import { useState, useEffect } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useAppStore } from '../stores/app'
import { ToastContainer, useToast } from './Toast'
import { api } from '../lib/api'
import { MessageSquare, List, Settings, User, Boxes } from 'lucide-react'

const NAV = [
  { path: '/', label: '对话', icon: MessageSquare },
  { path: '/projects', label: '项目', icon: Boxes },
  { path: '/tasks', label: '任务', icon: List },
  { path: '/config', label: '配置', icon: Settings },
]

const PHASE_CN: Record<string,string> = {
  template:'待开始', researching:'调研', gate1:'G1 审核', planning:'架构', gate2:'G2 审核',
  executing:'执行中', integrating:'集成', reviewing:'审查', fixing:'修复', gate3:'G3 审核', delivering:'交付', done:'完成'
}

function getPinned(): string[] {
  try { return JSON.parse(localStorage.getItem('qidian-pinned') || '[]') } catch { return [] }
}
function togglePin(pid: string) {
  const pins = getPinned()
  const next = pins.includes(pid) ? pins.filter(p => p !== pid) : [pid, ...pins]
  localStorage.setItem('qidian-pinned', JSON.stringify(next))
}

export default function AppLayout() {
  const { sidebarCollapsed, toggleSidebar } = useAppStore()
  const setActiveProject = useAppStore(s => s.setActiveProject)
  const activePid = useAppStore(s => s.activeProjectId)
  const navigate = useNavigate()
  const location = useLocation()
  const pathname = location.pathname
  const [projects, setProjects] = useState<any[]>([])
  const [hovered, setHovered] = useState<string>('')
  const [pinned, setPinned] = useState<string[]>(getPinned)
  const addToast = useToast(s => s.add)
  const sidebarWidth = sidebarCollapsed ? 0 : 260

  useEffect(() => {
    const f = async () => {
      try { const d: any = await api.projects(); setProjects(Array.isArray(d)?d:(d?.projects||[])) } catch { addToast('加载项目失败', 'error') }
    }
    f(); const t = setInterval(f, 10000); return () => clearInterval(t)
  }, [])

  const selectProject = (pid: string) => { setActiveProject(pid); navigate('/') }
  const deleteProject = async (p: any) => {
    if (confirm(`删除 "${p.name}"?`)) {
      await api.deleteProject(p.id)
      setProjects(prev => prev.filter(x => x.id !== p.id))
      if (activePid === p.id) { setActiveProject('_default'); navigate('/') }
    }
  }

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
                <div key={p.id} onClick={() => selectProject(p.id)}
                  onMouseEnter={() => setHovered(p.id)} onMouseLeave={() => setHovered('')}
                  style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '5px 8px', margin: '1px 0',
                    borderRadius: 6, cursor: 'pointer', fontSize: 12, color: isActive ? '#141413' : '#6b6b68',
                    background: isActive ? '#f3f2ec' : 'transparent' }}>
                  <span style={{ color: isPinned ? '#d97706' : '#b5b2a8', fontSize: 10 }}>#</span>
                  <span className="truncate" style={{ flex: 1 }}>{p.name}</span>
                  <span className="fs-10" style={{ color: '#b5b2a8' }}>{PHASE_CN[p.phase] || p.phase}</span>
                  {hovered === p.id && (
                    <span className="flex-center gap-4">
                      <button onClick={e => { e.stopPropagation(); togglePin(p.id); setPinned(getPinned()) }}
                        style={{ background:'none',border:'none',cursor:'pointer',padding:1,fontSize:10,color: isPinned?'#d97706':'#9a9993' }}>📌</button>
                      <button onClick={e => { e.stopPropagation(); deleteProject(p) }}
                        style={{ background:'none',border:'none',cursor:'pointer',padding:1,fontSize:10,color:'#9a9993' }}>×</button>
                    </span>
                  )}
                </div>
              )
            })}
        </div>

        <div style={{ padding: '8px 12px', borderTop: '1px solid #f3f2ec', display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ width: 22, height: 22, borderRadius: 11, background: '#d8d5cb', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <User size={12} style={{color:'#9a9993'}}/>
          </div>
          <span style={{ fontSize: 11, color: '#9a9993', flex: 1 }}>local</span>
          <button onClick={() => navigate('/config')} className="btn-icon" title="配置"><Settings size={14}/></button>
        </div>
      </div>

      {sidebarCollapsed && (
        <button onClick={toggleSidebar} style={{ position:'fixed',left:8,top:10,zIndex:10,background:'#f3f2ec',border:'none',borderRadius:6,color:'#9a9993',cursor:'pointer',padding:6 }}>
          <MessageSquare size={14}/>
        </button>
      )}

      <main className="main-area">
        <div className="main-scroll"><Outlet /></div>
      </main>
      <ToastContainer />
    </div>
  )
}
