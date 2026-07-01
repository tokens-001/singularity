import { useState, useEffect } from 'react'
import { Outlet, useNavigate } from 'react-router-dom'
import { useAppStore } from '../stores/app'
import { ToastContainer, useToast } from './Toast'
import { api } from '../lib/api'
import { MessageSquare, Plus, Search, List, Settings, User } from 'lucide-react'

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
  const [projects, setProjects] = useState<any[]>([])
  const [showSearch, setShowSearch] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [hovered, setHovered] = useState<string>('')
  const [pinned, setPinned] = useState<string[]>(getPinned)
  const addToast = useToast(s => s.add)
  const sidebarWidth = sidebarCollapsed ? 0 : 260

  useEffect(() => {
    const f = async () => {
      try { const d: any = await api.projects(); setProjects(Array.isArray(d)?d:(d?.projects||[])) } catch {}
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

  const filteredProjects = projects.filter(p => !searchQuery || p.name.includes(searchQuery))

  return (
    <div style={{ display: 'flex', height: '100vh', background: '#000' }}>
      {/* 左侧边栏 */}
      <div style={{ width: sidebarWidth, transition: 'width 0.15s', overflow: 'hidden', background: '#0a0a0a', borderRight: '1px solid #1c1c1e', display: 'flex', flexDirection: 'column', flexShrink: 0 }}>
        {/* 顶部功能按钮 (竖向, 对应奇点实际操作) */}
        <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 2 }}>
          <button onClick={() => { setActiveProject('_default'); navigate('/') }}
            style={{ ...sideBtn }}>
            <Plus size={14}/> 新建项目
          </button>
          <button onClick={() => navigate('/tasks')}
            style={{ ...sideBtn }}>
            <List size={14}/> 任务管理
          </button>
        </div>

        {/* 搜索 */}
        {showSearch && (
          <div style={{ padding: '0 12px 6px' }}>
            <input value={searchQuery} onChange={e => setSearchQuery(e.target.value)}
              placeholder="搜索..."
              style={{ width: '100%', background: '#1c1c1e', border: '1px solid #2c2c2e', borderRadius: 6, padding: '4px 8px', color: '#fff', fontSize: 12, outline: 'none' }}/>
          </div>
        )}

        {/* 项目列表标题 */}
        <div style={{ padding: '6px 12px 4px', fontSize: 10, color: '#555', fontWeight: 600 }}>
          项目
        </div>

        {/* 项目列表 */}
        <div style={{ flex: 1, overflow: 'auto', padding: '0 6px' }}>
          {[...filteredProjects].sort((a, b) => {
              const aPin = pinned.includes(a.id) ? 0 : 1
              const bPin = pinned.includes(b.id) ? 0 : 1
              return aPin - bPin || a.name.localeCompare(b.name)
            }).map((p: any) => {
              const isPinned = pinned.includes(p.id)
              const isActive = activePid === p.id
              return (
                <div key={p.id} onClick={() => selectProject(p.id)}
                  onMouseEnter={() => setHovered(p.id)} onMouseLeave={() => setHovered('')}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 6, padding: '5px 8px', margin: '1px 0',
                    borderRadius: 6, cursor: 'pointer', fontSize: 12, color: isActive ? '#fff' : '#999',
                    background: isActive ? '#1c1c1e' : 'transparent',
                  }}>
                  <span style={{ color: isPinned ? '#f0a060' : '#444', fontSize: 10 }}>#</span>
                  <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.name}</span>
                  <span style={{ fontSize: 10, color: '#444' }}>{p.phase === 'done' ? '完成' : p.phase === 'executing' ? '执行中' : p.phase}</span>
                  {hovered === p.id && (
                    <span style={{ display: 'flex', gap: 2 }}>
                      <button onClick={e => { e.stopPropagation(); togglePin(p.id); setPinned(getPinned()) }}
                        style={{ ...opBtn, color: isPinned ? '#f0a060' : '#555' }}>{isPinned ? '📌' : '📌'}</button>
                      <button onClick={e => { e.stopPropagation(); deleteProject(p) }}
                        style={{ ...opBtn, color: '#555' }}>×</button>
                    </span>
                  )}
                </div>
              )
            })}
          </div>

        {/* 底部: 用户 + 配置 */}
        <div style={{ padding: '8px 12px', borderTop: '1px solid #1c1c1e', display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ width: 22, height: 22, borderRadius: 11, background: '#333', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <User size={12} style={{color:'#666'}}/>
          </div>
          <span style={{ fontSize: 11, color: '#666', flex: 1 }}>奇点</span>
          <button onClick={() => navigate('/config')} style={iconBtn} title="配置">
            <Settings size={14}/>
          </button>
        </div>
      </div>

      {/* 折叠按钮 */}
      {sidebarCollapsed && (
        <button onClick={toggleSidebar} style={{ position:'fixed',left:8,top:10,zIndex:10,background:'#1c1c1e',border:'none',borderRadius:6,color:'#666',cursor:'pointer',padding:6 }}>
          <MessageSquare size={14}/>
        </button>
      )}

      {/* 主区域 */}
      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', background: '#000' }}>
        <div style={{ flex: 1, overflow: 'auto', padding: '10px 20px' }}><Outlet /></div>
      </main>
      <ToastContainer />
    </div>
  )
}

function FolderIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
    </svg>
  )
}

const sideBtn: React.CSSProperties = { background: 'none', border: 'none', color: '#999', cursor: 'pointer', fontSize: 12, padding: '4px 6px', borderRadius: 4, display: 'flex', alignItems: 'center', gap: 6, textAlign: 'left' as const, width: '100%' }
const shortcutHint: React.CSSProperties = { fontSize: 9, color: '#444', marginLeft: 'auto' }
const iconBtn: React.CSSProperties = { background: 'none', border: 'none', color: '#666', cursor: 'pointer', padding: 4, borderRadius: 4 }
const opBtn: React.CSSProperties = { background: 'none', border: 'none', cursor: 'pointer', padding: 1, fontSize: 10 }
