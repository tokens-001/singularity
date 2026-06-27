import { Outlet, NavLink, Link, useLocation } from 'react-router-dom'
import { useAppStore } from '../stores/app'
import { LayoutDashboard, ListTodo, MessageSquare, Bot, FolderKanban, Cpu, Settings2, PanelLeftClose, PanelLeft } from 'lucide-react'

const NAV = [
  { to: '/', icon: LayoutDashboard, label: '总览' },
  { to: '/tasks', icon: ListTodo, label: '任务' },
  { to: '/projects', icon: FolderKanban, label: '项目' },
  { to: '/observer', icon: MessageSquare, label: '对话' },
  { to: '/agents', icon: Bot, label: '智能体' },
  { to: '/models', icon: Cpu, label: '模型' },
]

export default function AppLayout() {
  const { sidebarCollapsed, toggleSidebar } = useAppStore()
  const { pathname } = useLocation()

  return (
    <div style={{ display: 'flex', height: '100vh' }}>
      <aside style={{
        width: sidebarCollapsed ? 48 : 160, transition: 'width 0.15s',
        background: 'var(--bg-secondary)', borderRight: '1px solid var(--border)',
        display: 'flex', flexDirection: 'column', overflow: 'hidden', flexShrink: 0,
      }}>
        <div style={{ padding: '12px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', gap: 8, minHeight: 48 }}>
          {!sidebarCollapsed && <span style={{ fontWeight: 700, fontSize: 15, color: 'var(--accent)' }}>奇点</span>}
          <button onClick={toggleSidebar} style={{ background: 'none', border: 'none',
            color: 'var(--text-secondary)', cursor: 'pointer', marginLeft: 'auto', padding: 2 }}>
            {sidebarCollapsed ? <PanelLeft size={16} /> : <PanelLeftClose size={16} />}
          </button>
        </div>
        <nav style={{ flex: 1, padding: 6 }}>
          {NAV.map(({ to, icon: Icon, label }) => (
            <NavLink key={to} to={to} end={to === '/'} style={({ isActive }) => ({
              display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px',
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
        <div style={{ padding: 0, borderTop: '1px solid var(--border)' }}>
          <NavLink to="/settings" style={({ isActive }) => ({
            display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px',
            color: isActive ? 'var(--text-primary)' : 'var(--text-muted)',
            background: isActive ? 'var(--bg-tertiary)' : 'transparent',
            textDecoration: 'none', fontSize: 13, fontWeight: isActive ? 600 : 400,
          })}>
            <Settings2 size={18} />{!sidebarCollapsed && '配置'}
          </NavLink>
        </div>
      </aside>
      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <header style={{
          height: 40, borderBottom: '1px solid var(--border)', background: 'var(--bg-secondary)',
          display: 'flex', alignItems: 'center', padding: '0 14px', gap: 10, flexShrink: 0,
        }}>
          <span style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{pathname}</span>
          <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-muted)' }}>Singularity</span>
        </header>
        <div style={{ flex: 1, overflow: 'auto', padding: 14 }}>
          <Outlet />
        </div>
      </main>
    </div>
  )
}
