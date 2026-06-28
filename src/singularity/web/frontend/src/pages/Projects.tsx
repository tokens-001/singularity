import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { Plus, RefreshCw, ChevronDown, ChevronRight } from 'lucide-react'

const PHASE_CN: Record<string,string> = {
  template:'模板', researching:'调研', gate1:'G1确认', planning:'规划', gate2:'G2确认',
  executing:'执行', integrating:'集成', reviewing:'审查', fixing:'修复', gate3:'G3确认', delivering:'交付', done:'完成'
}
const PC: Record<string,string> = {
  template:'var(--text-muted)', researching:'var(--accent)', gate1:'var(--accent-yellow)', planning:'var(--accent-purple)',
  gate2:'var(--accent-yellow)', executing:'var(--accent-green)', integrating:'var(--accent-green)',
  reviewing:'#f0883e', fixing:'var(--accent-red)', gate3:'var(--accent-yellow)', delivering:'var(--accent)', done:'var(--accent-green)'
}

export default function Projects() {
  const [projects, setProjects] = useState<any[]>([])
  const [expanded, setExpanded] = useState<string|null>(null)
  const [detail, setDetail] = useState<any>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [form, setForm] = useState({ name: '', description: '', template: 'product_dev' })
  const [loading, setLoading] = useState(true)

  const fetch = async () => {
    setLoading(true)
    try { const d: any = await api.projects(); setProjects(Array.isArray(d)?d:(d?.projects||[])) } catch {}
    setLoading(false)
  }
  useEffect(() => { fetch() }, [])
  useSSE(() => { fetch() })

  const toggle = async (id: string) => {
    if (expanded === id) { setExpanded(null); setDetail(null); return }
    setExpanded(id)
    try { const d = await api.project(id); setDetail(d) } catch {}
  }

  const create = async () => {
    if (!form.name) return
    await api.createProject(form)
    setShowCreate(false); setForm({ name: '', description: '', template: 'product_dev' }); fetch()
  }

  const phases = ['template','researching','gate1','planning','gate2','executing','integrating','reviewing','fixing','gate3','delivering','done']

  return (
    <div style={{ maxWidth: 900, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600 }}>项目</h2>
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{projects.length} 个</span>
        <span style={{ flex: 1 }} />
        <button onClick={fetch} style={{background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2}}><RefreshCw size={14}/></button>
        <button onClick={()=>setShowCreate(!showCreate)} style={{background:'var(--accent-green)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'6px 12px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:4}}><Plus size={14}/> 新建</button>
      </div>

      {showCreate && (
        <div style={{ display: 'flex', gap: 8, marginBottom: 10, padding: 10, background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', flexWrap: 'wrap' }}>
          <input value={form.name} onChange={e=>setForm({...form,name:e.target.value})} placeholder="项目名称" style={{background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:4,padding:'6px 8px',color:'var(--text-primary)',fontSize:12,flex:1}} />
          <input value={form.description} onChange={e=>setForm({...form,description:e.target.value})} placeholder="需求描述" style={{background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:4,padding:'6px 8px',color:'var(--text-primary)',fontSize:12,flex:2}} />
          <select value={form.template} onChange={e=>setForm({...form,template:e.target.value})} style={{background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:4,padding:'6px 8px',color:'var(--text-primary)',fontSize:12,width:'auto'}}>
            <option value="product_dev">产品开发</option><option value="bug_fix">Bug修复</option><option value="refactor">重构</option>
          </select>
          <button onClick={create} style={{ background: 'var(--accent-green)', color: '#fff', border: 'none', borderRadius: 4, padding: '6px 14px', cursor: 'pointer', fontSize: 12 }}>创建</button>
        </div>
      )}

      {loading && <div style={{ color: 'var(--text-muted)', fontSize: 12, padding: 20 }}>加载中...</div>}
      {!loading && projects.map((p: any) => (
        <div key={p.id} style={{ marginBottom: 6 }}>
          <div onClick={()=>toggle(p.id)}
            style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', cursor: 'pointer' }}>
            <span style={{ color: 'var(--text-muted)' }}>{expanded===p.id?<ChevronDown size={12}/>:<ChevronRight size={12}/>}</span>
            <span style={{ fontWeight: 600, fontSize: 13, flex: 1 }}>{p.name}</span>
            <span style={{ fontSize: 10, color: PC[p.phase]||'var(--text-muted)', fontWeight: 600 }}>{PHASE_CN[p.phase]||p.phase}</span>
            <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{p.task_count||0} 任务</span>
          </div>
          {expanded === p.id && detail && (
            <div style={{ marginLeft: 20, padding: '8px 14px', borderLeft: '1px solid var(--border)', fontSize: 12 }}>
              <div style={{ display: 'flex', gap: 2, marginBottom: 8, flexWrap: 'wrap' }}>
                {phases.map(ph => (
                  <span key={ph} style={{
                    padding: '2px 6px', borderRadius: 3, fontSize: 9, fontWeight: ph===detail.phase?700:400,
                    color: ph===detail.phase?'#fff':(PC[ph]||'var(--text-muted)'),
                    background: ph===detail.phase?PC[ph]:'transparent',
                    border: '1px solid '+(PC[ph]||'var(--border)')
                  }}>{PHASE_CN[ph]||ph}</span>
                ))}
              </div>
              <div style={{ color: 'var(--text-secondary)', marginBottom: 4 }}>{detail.description}</div>
              {detail.lineage && detail.lineage.length > 0 && (
                <div style={{ marginTop: 4, fontSize: 10, color: 'var(--text-muted)' }}>
                  {detail.lineage.slice(-5).map((l:any,i:number) => (
                    <div key={i}>[{l.action}] {l.agent||''} {l.task_count?l.task_count+'任务':''}</div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
