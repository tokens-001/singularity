import { useState, useEffect } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useAppStore } from '../stores/app'
import { ToastContainer, useToast } from './Toast'
import { api } from '../lib/api'
import { MessageSquare, PanelLeftClose, PanelLeft, Plus, Trash2, Pin, PinOff } from 'lucide-react'

// 置顶存储 (ponytail: localStorage 直读, 不引入额外 store)
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
  const { pathname } = useLocation()
  const [projects, setProjects] = useState<any[]>([])
  const [hovered, setHovered] = useState<string>('')
  const [pinned, setPinned] = useState<string[]>(getPinned)
  const addToast = useToast(s => s.add)

  useEffect(() => {
    const fetch = async () => {
      try { const d: any = await api.projects(); setProjects(Array.isArray(d)?d:(d?.projects||[])) } catch {}
    }
    fetch()
    const t = setInterval(fetch, 10000)
    return () => clearInterval(t)
  }, [])

  const selectProject = (pid: string) => {
    setActiveProject(pid)
    navigate('/')
  }

  return (
    <div style={{ display: 'flex', height: '100vh', background: 'var(--bg-primary)' }}>
      {/* 左侧: 项目列表 */}
      <aside style={{
        width: sidebarCollapsed ? 0 : 220, transition: 'width 0.15s', overflow: 'hidden',
        background: 'var(--bg-secondary)', borderRight: '1px solid var(--border)',
        display: 'flex', flexDirection: 'column', flexShrink: 0,
      }}>
        {/* 顶栏: 标题 + 折叠按钮 */}
        <div style={{ padding: '12px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', gap: 8, minHeight: 44 }}>
          <MessageSquare size={16} style={{ color: 'var(--accent)' }}/>
          <span style={{ fontWeight: 700, fontSize: 14, color: 'var(--text-primary)', flex: 1 }}>奇点</span>
          <button onClick={toggleSidebar} style={{ background: 'none', border: 'none', color: 'var(--text-secondary)', cursor: 'pointer', padding: 2 }}>
            <PanelLeftClose size={14}/>
          </button>
        </div>

        {/* 通用对话 */}
        <button onClick={() => selectProject('_default')}
          style={{
            display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', margin: '4px 6px',
            border: 'none', borderRadius: 'var(--radius)', cursor: 'pointer', fontSize: 13,
            background: activePid === '_default' ? 'var(--bg-tertiary)' : 'transparent',
            color: activePid === '_default' ? 'var(--text-primary)' : 'var(--text-secondary)',
            textAlign: 'left' as const, width: 'auto',
          }}>
          <MessageSquare size={14}/>
          <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>通用对话</span>
        </button>

        {/* 项目列表 */}
        <div style={{ padding: '4px 12px', fontSize: 10, color: 'var(--text-muted)', fontWeight: 600, marginTop: 4 }}>
          项目
        </div>
        {/* 排序: 置顶优先 */}
        {[...projects].sort((a, b) => {
          const aPin = pinned.includes(a.id) ? 0 : 1
          const bPin = pinned.includes(b.id) ? 0 : 1
          return aPin - bPin || a.name.localeCompare(b.name)
        }).map((p: any) => {
          const isPinned = pinned.includes(p.id)
          return (
          <div key={p.id} onClick={() => selectProject(p.id)}
            onMouseEnter={() => setHovered(p.id)} onMouseLeave={() => setHovered('')}
            style={{
              display: 'flex', alignItems: 'center', gap: 6, padding: '5px 8px 5px 12px', margin: '1px 6px',
              border: 'none', borderRadius: 'var(--radius)', cursor: 'pointer', fontSize: 12,
              background: activePid === p.id ? 'var(--bg-tertiary)' : 'transparent',
              color: activePid === p.id ? 'var(--text-primary)' : 'var(--text-secondary)',
              textAlign: 'left' as const, width: 'auto',
            }}>
            <span style={{ fontSize: 8, color: isPinned ? 'var(--accent-yellow)' : 'var(--text-muted)' }}>#</span>
            <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.name}</span>
            <span style={{ fontSize: 9, color: 'var(--text-muted)' }}>{p.phase}</span>
            {/* 操作按钮: hover 时显示 */}
            {hovered === p.id && (
              <>
                <button onClick={e => { e.stopPropagation(); togglePin(p.id); setPinned(getPinned()) }}
                  style={{ ...opBtn, color: isPinned ? 'var(--accent-yellow)' : 'var(--text-muted)' }}
                  title={isPinned ? '取消置顶' : '置顶'}>
                  {isPinned ? <PinOff size={10}/> : <Pin size={10}/>}
                </button>
                <button onClick={e => {
                  e.stopPropagation()
                  if (confirm(`删除项目 "${p.name}"?`)) {
                    api.deleteProject(p.id).then(() => {
                      setProjects(prev => prev.filter(x => x.id !== p.id))
                      if (activePid === p.id) { setActiveProject('_default'); navigate('/') }
                      addToast(`已删除 ${p.name}`, 'success')
                    }).catch(() => addToast('删除失败', 'error'))
                  }
                }} style={{ ...opBtn, color: 'var(--accent-red)' }} title="删除">
                  <Trash2 size={10}/>
                </button>
              </>
            )}
          </div>
        )})}

        {/* 底部: 配置入口 */}
        <div style={{ marginTop: 'auto', padding: '8px 12px', borderTop: '1px solid var(--border)' }}>
          <button onClick={() => { navigate('/tasks') }}
            style={{ display: 'block', width: '100%', padding: '4px 8px', border: 'none', borderRadius: 4,
              cursor: 'pointer', fontSize: 11, color: 'var(--text-secondary)', background: 'transparent', textAlign: 'left' as const }}>
            任务管理
          </button>
          <button onClick={() => { navigate('/config') }}
            style={{ display: 'block', width: '100%', padding: '4px 8px', border: 'none', borderRadius: 4,
              cursor: 'pointer', fontSize: 11, color: 'var(--text-secondary)', background: 'transparent', textAlign: 'left' as const }}>
            配置
          </button>
        </div>
      </aside>

      {/* 折叠状态的小按钮 */}
      {sidebarCollapsed && (
        <button onClick={toggleSidebar}
          style={{ position: 'fixed', left: 8, top: 10, zIndex: 10, background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', color: 'var(--text-secondary)', cursor: 'pointer', padding: 6 }}>
          <PanelLeft size={14}/>
        </button>
      )}

      {/* 主内容区 */}
      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <div style={{ flex: 1, overflow: 'auto', padding: '10px 14px' }}>
          <Outlet />
        </div>
      </main>
      <ToastContainer />
    </div>
  )
}

const opBtn: React.CSSProperties = { background: 'none', border: 'none', cursor: 'pointer', padding: 2 }
