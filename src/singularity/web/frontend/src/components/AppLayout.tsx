import { Outlet, NavLink, useLocation } from 'react-router-dom'
import { useAppStore } from '../stores/app'
import { ListTodo, MessageSquare, Bot, GitBranch, PanelLeftClose, PanelLeft } from 'lucide-react'

const NAV = [
  { to: '/tasks', icon: ListTodo, label: '任务' },
  { to: '/observer', icon: MessageSquare, label: '对话' },
  { to: '/agents', icon: Bot, label: 'Agent' },
]

export default function AppLayout() {
  const { sidebarCollapsed, toggleSidebar } = useAppStore()
  const { pathname } = useLocation()

  return (
    <div style={{ display: 'flex', height: '100vh' }}>
      {/* Sidebar */}
      <aside style={{
        width: sidebarCollapsed ? 48 : 200, transition: 'width 0.2s',
        background: 'var(--bg-secondary)', borderRight: '1px solid var(--border)',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        <div style={{ padding: '12px 14px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', gap: 8, minHeight: 48 }}>
          {!sidebarCollapsed && <span style={{ fontWeight: 700, fontSize: 15, color: 'var(--accent)' }}>奇点</span>}
          <button onClick={toggleSidebar} style={{ background: 'none', border: 'none',
            color: 'var(--text-secondary)', cursor: 'pointer', marginLeft: 'auto', padding: 2 }}>
            {sidebarCollapsed ? <PanelLeft size={16} /> : <PanelLeftClose size={16} />}
          </button>
        </div>
        <nav style={{ flex: 1, padding: 8 }}>
          {NAV.map(({ to, icon: Icon, label }) => (
            <NavLink key={to} to={to} style={({ isActive }) => ({
              display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px',
              borderRadius: 'var(--radius)', marginBottom: 2,
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

      {/* Main */}
      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <header style={{
          height: 48, borderBottom: '1px solid var(--border)',
          background: 'var(--bg-secondary)', display: 'flex', alignItems: 'center',
          padding: '0 16px', gap: 12, flexShrink: 0,
        }}>
          <span style={{ fontSize: 12, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
            {pathname}
          </span>
          <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-muted)' }}>
            Singularity v2
          </span>
        </header>
        <div style={{ flex: 1, overflow: 'auto', padding: 16 }}>
          <Outlet />
        </div>
      </main>
    </div>
  )
}
