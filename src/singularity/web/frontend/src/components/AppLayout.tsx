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
  const [showCreate, setShowCreate] = useState(false)
  const [createForm, setCreateForm] = useState({ name: '', description: '', template: 'feature' })
  const [creating, setCreating] = useState(false)
  const addToast = useToast(s => s.add)
  const sidebarWidth = sidebarCollapsed ? 0 : 260

  useEffect(() => {
    const f = async () => {
      try { const d: any = await api.projects(); setProjects(Array.isArray(d)?d:(d?.projects||[])) } catch { addToast('加载项目失败', 'error') }
    }
    f(); const t = setInterval(f, 10000); return () => clearInterval(t)
  }, [])

  const selectProject = (pid: string) => { setActiveProject(pid); navigate('/') }
  const handleCreate = async () => {
    if (!createForm.name || creating) return
    setCreating(true)
    try {
      const r: any = await api.createProject(createForm)
      if (r?.project?.id) {
        addToast('项目已创建', 'success')
        setActiveProject(r.project.id); navigate('/')
        setShowCreate(false); setCreateForm({ name: '', description: '', template: 'feature' })
      }
    } catch { addToast('创建失败', 'error') }
    setCreating(false)
  }
  const deleteProject = async (p: any) => {
    if (confirm(`删除 "${p.name}"?`)) {
      await api.deleteProject(p.id)
      setProjects(prev => prev.filter(x => x.id !== p.id))
      if (activePid === p.id) { setActiveProject('_default'); navigate('/') }
    }
  }

  const filteredProjects = projects.filter(p => !searchQuery || p.name.includes(searchQuery))

  return (
    <div className="app-shell">
      <div className="sidebar" style={{ width: sidebarWidth }}>
        <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 2 }}>
          <button onClick={() => setShowCreate(!showCreate)} className="btn-side">
            <Plus size={14}/> 新建项目
          </button>
          <button onClick={() => navigate('/tasks')} className="btn-side">
            <List size={14}/> 任务管理
          </button>
        </div>

        {showSearch && (
          <div style={{ padding: '0 12px 6px' }}>
            <input value={searchQuery} onChange={e => setSearchQuery(e.target.value)}
              placeholder="搜索..." className="inp-dark"/>
          </div>
        )}

        {showCreate && (
          <div style={{ padding: '0 12px 8px' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, padding: 8, background: '#1c1c1e', borderRadius: 8 }}>
              <input value={createForm.name} onChange={e=>setCreateForm({...createForm,name:e.target.value})}
                placeholder="项目名称" className="inp-dark"
                onKeyDown={e=>{if(e.key==='Enter'&&createForm.name){e.preventDefault();handleCreate()}}}/>
              <input value={createForm.description} onChange={e=>setCreateForm({...createForm,description:e.target.value})}
                placeholder="需求描述（可选）" className="inp-dark"/>
              <div style={{ display: 'flex', gap: 4 }}>
                <button onClick={handleCreate} disabled={!createForm.name||creating}
                  style={{ flex:1,background: createForm.name?'#fff':'#333',color: createForm.name?'#000':'#666',border:'none',borderRadius:4,padding:'5px 8px',cursor: createForm.name?'pointer':'default',fontSize:11,fontWeight:600 }}>
                  {creating?'创建中...':'创建'}
                </button>
                <button onClick={()=>setShowCreate(false)}
                  style={{ background:'none',border:'1px solid #333',borderRadius:4,padding:'5px 8px',color:'#666',cursor:'pointer',fontSize:11 }}>取消</button>
              </div>
            </div>
          </div>
        )}

        <div style={{ padding: '6px 12px 4px', fontSize: 10, color: '#555', fontWeight: 600 }}>项目</div>
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
                  style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '5px 8px', margin: '1px 0',
                    borderRadius: 6, cursor: 'pointer', fontSize: 12, color: isActive ? '#fff' : '#999',
                    background: isActive ? '#1c1c1e' : 'transparent' }}>
                  <span style={{ color: isPinned ? '#f0a060' : '#444', fontSize: 10 }}>#</span>
                  <span className="truncate" style={{ flex: 1 }}>{p.name}</span>
                  <span className="fs-10" style={{ color: '#444' }}>{p.phase === 'done' ? '完成' : p.phase === 'executing' ? '执行中' : p.phase}</span>
                  {hovered === p.id && (
                    <span className="flex-center gap-4">
                      <button onClick={e => { e.stopPropagation(); togglePin(p.id); setPinned(getPinned()) }}
                        style={{ background:'none',border:'none',cursor:'pointer',padding:1,fontSize:10,color: isPinned?'#f0a060':'#555' }}>📌</button>
                      <button onClick={e => { e.stopPropagation(); deleteProject(p) }}
                        style={{ background:'none',border:'none',cursor:'pointer',padding:1,fontSize:10,color:'#555' }}>×</button>
                    </span>
                  )}
                </div>
              )
            })}
        </div>

        <div style={{ padding: '8px 12px', borderTop: '1px solid #1c1c1e', display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ width: 22, height: 22, borderRadius: 11, background: '#333', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <User size={12} style={{color:'#666'}}/>
          </div>
          <span style={{ fontSize: 11, color: '#666', flex: 1 }}>奇点</span>
          <button onClick={() => navigate('/config')} className="btn-icon" title="配置"><Settings size={14}/></button>
        </div>
      </div>

      {sidebarCollapsed && (
        <button onClick={toggleSidebar} style={{ position:'fixed',left:8,top:10,zIndex:10,background:'#1c1c1e',border:'none',borderRadius:6,color:'#666',cursor:'pointer',padding:6 }}>
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
