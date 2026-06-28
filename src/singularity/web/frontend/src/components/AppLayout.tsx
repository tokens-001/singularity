import { Outlet, NavLink, useLocation } from 'react-router-dom'
import { useAppStore } from '../stores/app'
import { MessageSquare, ListTodo, FolderKanban, Settings2, PanelLeftClose, PanelLeft } from 'lucide-react'

const NAV = [
  { to: '/', icon: MessageSquare, label: '对话' },
  { to: '/tasks', icon: ListTodo, label: '任务' },
  { to: '/projects', icon: FolderKanban, label: '项目' },
  { to: '/config', icon: Settings2, label: '配置' },
]

export default function AppLayout() {
  const { sidebarCollapsed, toggleSidebar } = useAppStore()
  const { pathname } = useLocation()

  return (
    <div style={{ display: 'flex', height: '100vh' }}>
      <aside style={{
        width: sidebarCollapsed ? 48 : 120, transition: 'width 0.15s',
        background: 'var(--bg-secondary)', borderRight: '1px solid var(--border)',
        display: 'flex', flexDirection: 'column', overflow: 'hidden', flexShrink: 0,
      }}>
        <div style={{ padding: '10px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', gap: 6, minHeight: 40 }}>
          {!sidebarCollapsed && <span style={{ fontWeight: 700, fontSize: 14, color: 'var(--accent)' }}>奇点</span>}
          <button onClick={toggleSidebar} style={{ background: 'none', border: 'none',
            color: 'var(--text-secondary)', cursor: 'pointer', marginLeft: 'auto', padding: 2 }}>
            {sidebarCollapsed ? <PanelLeft size={16} /> : <PanelLeftClose size={16} />}
          </button>
        </div>
        <nav style={{ flex: 1, padding: 4 }}>
          {NAV.map(({ to, icon: Icon, label }) => (
            <NavLink key={to} to={to} end={to === '/'} style={({ isActive }) => ({
              display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px',
              borderRadius: 'var(--radius)', marginBottom: 1,
              background: isActive ? 'var(--bg-tertiary)' : 'transparent',
              color: isActive ? 'var(--text-primary)' : 'var(--text-secondary)',
              textDecoration: 'none', fontSize: 13, fontWeight: isActive ? 600 : 400,
            })}>
              <Icon size={18} />
              {!sidebarCollapsed && label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <div style={{ flex: 1, overflow: 'auto', padding: 14 }}>
          <Outlet />
        </div>
      </main>
    </div>
  )
}
