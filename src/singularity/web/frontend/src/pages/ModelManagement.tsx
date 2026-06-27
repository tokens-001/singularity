import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Cpu, Trash2, Plus, Edit2, Download } from 'lucide-react'

export default function ModelManagement() {
  const [models, setModels] = useState<any[]>([])
  const [showForm, setShowForm] = useState(false)
  const [editId, setEditId] = useState('')
  const [form, setForm] = useState<any>({ id: '', model: '', provider: '', tier: '', context_window: 0, cost_1k_input: 0, cost_1k_output: 0 })

  const fetch = () => api.models().then(setModels)
  useEffect(() => { fetch() }, [])

  const handleSave = async () => {
    if (editId) {
      await api.updateModel(editId, form)
    } else {
      await api.addModel(form)
    }
    setShowForm(false); setEditId(''); setForm({}); fetch()
  }

  const handleEdit = (m: any) => {
    setEditId(m.id || m.model)
    setForm(m)
    setShowForm(true)
  }

  return (
    <div>
      <div style={{display:'flex',alignItems:'center',marginBottom:14}}>
        <h2 style={{fontSize:16,fontWeight:600}}>模型管理 ({models.length})</h2>
        <button onClick={() => { setEditId(''); setForm({}); setShowForm(!showForm) }}
          style={{marginLeft:'auto',background:'var(--accent)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'6px 12px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:4}}>
          <Plus size={14}/> 添加模型</button>
      </div>

      {showForm && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14,marginBottom:14}}>
          <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:8}}>
            <input placeholder="模型ID" value={form.id||''} onChange={e=>setForm({...form,id:e.target.value})} style={s}/>
            <input placeholder="Provider" value={form.provider||''} onChange={e=>setForm({...form,provider:e.target.value})} style={s}/>
            <input placeholder="Tier (E/E+/D)" value={form.tier||''} onChange={e=>setForm({...form,tier:e.target.value})} style={s}/>
            <input placeholder="Context Window" type="number" value={form.context_window||''} onChange={e=>setForm({...form,context_window:parseInt(e.target.value)})} style={s}/>
            <input placeholder="Cost input/1k" type="number" value={form.cost_1k_input||''} onChange={e=>setForm({...form,cost_1k_input:parseFloat(e.target.value)})} style={s}/>
            <input placeholder="Cost output/1k" type="number" value={form.cost_1k_output||''} onChange={e=>setForm({...form,cost_1k_output:parseFloat(e.target.value)})} style={s}/>
          </div>
          <div style={{display:'flex',gap:8,marginTop:10}}>
            <button onClick={handleSave} style={{background:'var(--accent-green)',color:'#fff',border:'none',borderRadius:4,padding:'6px 14px',cursor:'pointer',fontSize:12}}>{editId?'更新':'添加'}</button>
            <button onClick={()=>setShowForm(false)} style={{background:'var(--bg-tertiary)',color:'var(--text-secondary)',border:'none',borderRadius:4,padding:'6px 14px',cursor:'pointer',fontSize:12}}>取消</button>
          </div>
        </div>
      )}

      <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(300px,1fr))',gap:10}}>
        {models.map(m=>(
          <div key={m.id||m.model} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
            <div style={{display:'flex',justifyContent:'space-between',alignItems:'start'}}>
              <div>
                <div style={{fontWeight:600,fontSize:13,display:'flex',alignItems:'center',gap:6}}>
                  <Cpu size={14} color='var(--accent)'/>{m.id||m.model}</div>
                <div style={{fontSize:11,color:'var(--text-secondary)',marginTop:2}}>{m.provider} · {m.tier}</div>
              </div>
              <div style={{display:'flex',gap:4}}>
                <button onClick={()=>handleEdit(m)} style={{background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2}}><Edit2 size={14}/></button>
                <button onClick={()=>api.deleteModel(m.id||m.model).then(fetch)} style={{background:'none',border:'none',color:'var(--text-muted)',cursor:'pointer',padding:2}}><Trash2 size={14}/></button>
              </div>
            </div>
            {m.context_window && <div style={{fontSize:10,color:'var(--text-muted)',marginTop:4}}>上下文: {m.context_window}</div>}
            {m.cost_1k_input && <div style={{fontSize:10,color:'var(--text-muted)'}}>${m.cost_1k_input}/1k in · ${m.cost_1k_output}/1k out</div>}
          </div>
        ))}
      </div>
    </div>
  )
}
const s = { background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:4,padding:'6px 8px',color:'var(--text-primary)',fontSize:12 } as const
