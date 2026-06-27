import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Wrench, Plus, Trash2, RefreshCw } from 'lucide-react'

export default function SkillManagement() {
  const [skills, setSkills] = useState<any[]>([])
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({ name: '', description: '', type: 'prompt', content: '' })
  const [selectedSkill, setSelectedSkill] = useState<any>(null)

  const fetch = () => api.skills().then(setSkills)
  useEffect(() => { fetch() }, [])

  const handleAdd = async () => {
    await api.addSkill(form)
    setShowForm(false); setForm({ name: '', description: '', type: 'prompt', content: '' }); fetch()
  }

  return (
    <div>
      <div style={{display:'flex',alignItems:'center',marginBottom:14}}>
        <h2 style={{fontSize:16,fontWeight:600}}>Skill 管理 ({skills.length})</h2>
        <button onClick={()=>setShowForm(!showForm)}
          style={{marginLeft:'auto',background:'var(--accent)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'6px 12px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:4}}>
          <Plus size={14}/> 添加 Skill</button>
      </div>

      {showForm && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14,marginBottom:14,display:'flex',flexDirection:'column',gap:8}}>
          <input placeholder="Skill 名称" value={form.name} onChange={e=>setForm({...form,name:e.target.value})} style={s}/>
          <input placeholder="描述" value={form.description} onChange={e=>setForm({...form,description:e.target.value})} style={s}/>
          <textarea placeholder="内容" value={form.content} onChange={e=>setForm({...form,content:e.target.value})} style={{...s,minHeight:80}}/>
          <button onClick={handleAdd} style={{background:'var(--accent-green)',color:'#fff',border:'none',borderRadius:4,padding:'6px 12px',cursor:'pointer',fontSize:12,width:80}}>保存</button>
        </div>
      )}

      <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(300px,1fr))',gap:10}}>
        {skills.map(s=>(
          <div key={s.name} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14,cursor:'pointer'}}
            onClick={()=>setSelectedSkill(selectedSkill?.name===s.name?null:s)}>
            <div style={{display:'flex',justifyContent:'space-between',alignItems:'start'}}>
              <div>
                <div style={{fontWeight:600,fontSize:13,display:'flex',alignItems:'center',gap:6}}>
                  <Wrench size={14} color='var(--accent-purple)'/>{s.name}</div>
                <div style={{fontSize:11,color:'var(--text-secondary)',marginTop:2}}>{s.description||s.type}</div>
              </div>
              <button onClick={e=>{e.stopPropagation();api.deleteSkill(s.name).then(fetch)}}
                style={{background:'none',border:'none',color:'var(--text-muted)',cursor:'pointer',padding:2}}><Trash2 size={14}/></button>
            </div>
            {selectedSkill?.name===s.name && (
              <pre style={{fontSize:10,color:'var(--text-secondary)',marginTop:8,whiteSpace:'pre-wrap',maxHeight:200,overflow:'auto',background:'var(--bg-primary)',padding:8,borderRadius:4}}>
                {s.content||JSON.stringify(s,null,2)}</pre>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
const s = { background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:4,padding:'6px 8px',color:'var(--text-primary)',fontSize:12 } as const
