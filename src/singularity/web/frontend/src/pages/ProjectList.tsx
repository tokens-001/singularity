import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { Plus, Play, CheckCircle, ChevronRight } from 'lucide-react'

const PHASE_COLORS: Record<string,string> = {
  GATE1:'var(--accent-yellow)',GATE2:'var(--accent-yellow)',GATE3:'var(--accent-yellow)',
  RESEARCHING:'var(--accent)',PLANNING:'var(--accent)',EXECUTING:'var(--accent-green)',TEMPLATE:'var(--text-muted)',
}

export default function ProjectList() {
  const nav = useNavigate()
  const [projects, setProjects] = useState<any[]>([])
  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [scope, setScope] = useState('')
  const [template, setTemplate] = useState('product_dev')

  useEffect(() => { api.projects().then(setProjects) }, [])

  const handleCreate = () => {
    if (!desc.trim()) return
    api.createProject({ name, description: desc, scope, template }).then(() => {
      setShowCreate(false); setName(''); setDesc(''); setScope(''); api.projects().then(setProjects)
    })
  }

  const handleGate = async (p: any, gate: string, verdict: string) => {
    await api.gateConfirm(p.id, gate, verdict)
    api.projects().then(setProjects)
  }

  return (
    <div>
      <div style={{display:'flex',alignItems:'center',marginBottom:14}}>
        <h2 style={{fontSize:16,fontWeight:600}}>项目列表</h2>
        <button onClick={()=>setShowCreate(!showCreate)} style={{marginLeft:'auto',background:'var(--accent)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'6px 12px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:4}}>
          <Plus size={14}/> 新建项目</button>
      </div>

      {showCreate && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14,marginBottom:14,display:'flex',flexDirection:'column',gap:8}}>
          <input placeholder="项目名称" value={name} onChange={e=>setName(e.target.value)} style={inputStyle}/>
          <input placeholder="项目描述" value={desc} onChange={e=>setDesc(e.target.value)} style={inputStyle}/>
          <input placeholder="项目范围" value={scope} onChange={e=>setScope(e.target.value)} style={inputStyle}/>
          <select value={template} onChange={e=>setTemplate(e.target.value)} style={{...inputStyle,width:200}}>
            {['product_dev','agent_dev','bug_fix','refactor'].map(t=><option key={t} value={t}>{t}</option>)}
          </select>
          <button onClick={handleCreate} style={{background:'var(--accent-green)',color:'#fff',border:'none',borderRadius:4,padding:'8px',cursor:'pointer',fontSize:12,width:100}}>创建</button>
        </div>
      )}

      <div style={{display:'grid',gap:10}}>
        {projects.map(p=>(
          <div key={p.id} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14,cursor:'pointer'}}
            onClick={()=>nav(`/projects/${p.id}/pipeline`)}>
            <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
              <div>
                <div style={{fontWeight:600,fontSize:14}}>{p.name || '未命名'}</div>
                <div style={{fontSize:12,color:'var(--text-secondary)',marginTop:2}}>{p.description?.slice(0,100) || p.id}</div>
              </div>
              <div style={{display:'flex',alignItems:'center',gap:10}}>
                <span style={{fontSize:11,color:PHASE_COLORS[p.phase]||'var(--text-muted)',fontWeight:600}}>{p.phase}</span>
                {p.phase === 'GATE1' && <><button onClick={e=>{e.stopPropagation();handleGate(p,'GATE1','approved')}} style={gateBtn('var(--accent-green)')}><CheckCircle size={12}/> 通过</button><button onClick={e=>{e.stopPropagation();handleGate(p,'GATE1','rejected')}} style={gateBtn('var(--accent-red)')}>打回</button></>}
                {p.phase === 'GATE2' && <><button onClick={e=>{e.stopPropagation();handleGate(p,'GATE2','approved')}} style={gateBtn('var(--accent-green)')}><CheckCircle size={12}/> 通过</button><button onClick={e=>{e.stopPropagation();handleGate(p,'GATE2','rejected')}} style={gateBtn('var(--accent-red)')}>打回</button></>}
                {p.phase === 'TEMPLATE' && <button onClick={e=>{e.stopPropagation();api.runPhase(p.id).then(()=>api.projects().then(setProjects))}} style={gateBtn('var(--accent)')}><Play size={12}/> 启动</button>}
                <ChevronRight size={16} color='var(--text-muted)'/>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

const inputStyle = { background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:4,padding:'8px 10px',color:'var(--text-primary)',fontSize:13 } as const
const gateBtn = (color:string) => ({ background:color+'22',color,border:`1px solid ${color}44`,borderRadius:4,padding:'3px 8px',cursor:'pointer',fontSize:11,display:'flex',alignItems:'center',gap:3 })
