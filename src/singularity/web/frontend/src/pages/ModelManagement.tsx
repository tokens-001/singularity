import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Cpu, Trash2, Plus, Edit3, X, Check } from 'lucide-react'

export default function ModelManagement() {
  const [models, setModels] = useState<any[]>([])
  const [editId, setEditId] = useState<string | null>(null)
  const [editForm, setEditForm] = useState<any>({})
  const [addForm, setAddForm] = useState<any>({ id: '', provider: '', tier: '', context_window: '', cost_1k_input: '', cost_1k_output: '' })
  const [tierFilter, setTierFilter] = useState('')
  const [showAdd, setShowAdd] = useState(false)

  const fetch = () => api.models().then(setModels)
  useEffect(() => { fetch() }, [])

  const filtered = tierFilter ? models.filter(m => m.tier === tierFilter) : models

  const startEdit = (m: any) => {
    setEditId(m.id || m.model)
    setEditForm({
      id: m.id || m.model || '',
      provider: m.provider || '',
      tier: m.tier || '',
      context_window: m.context_window || '',
      cost_1k_input: m.cost_1k_input || '',
      cost_1k_output: m.cost_1k_output || '',
    })
  }

  const saveEdit = async () => {
    if (!editId) return
    await api.updateModel(editId, editForm)
    setEditId(null); fetch()
  }

  const handleAdd = async () => {
    if (!addForm.id.trim()) return
    await api.addModel({ ...addForm, context_window: parseInt(addForm.context_window) || 0, cost_1k_input: parseFloat(addForm.cost_1k_input) || 0, cost_1k_output: parseFloat(addForm.cost_1k_output) || 0 })
    setAddForm({ id: '', provider: '', tier: '', context_window: '', cost_1k_input: '', cost_1k_output: '' })
    setShowAdd(false); fetch()
  }

  const tiers = ['D', 'E+', 'E']

  return (
    <div>
      <div style={{display:'flex',alignItems:'center',marginBottom:14}}>
        <h2 style={{fontSize:16,fontWeight:600}}>模型管理 ({models.length})</h2>
        <div style={{display:'flex',gap:6,marginLeft:16}}>
          <button onClick={()=>setTierFilter('')} style={filterBtn(tierFilter==='')}>全部</button>
          {tiers.map(t=><button key={t} onClick={()=>setTierFilter(t)} style={filterBtn(tierFilter===t)}>{t} 层</button>)}
        </div>
        <button onClick={()=>setShowAdd(!showAdd)}
          style={{marginLeft:'auto',background:'var(--accent)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'6px 12px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:4}}>
          <Plus size={14}/> 添加模型</button>
      </div>

      {/* Add form — inline compact */}
      {showAdd && (
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--accent)',borderRadius:'var(--radius)',padding:'10px 14px',marginBottom:14,display:'flex',alignItems:'center',gap:10,flexWrap:'wrap'}}>
          <input placeholder="模型 ID" value={addForm.id} onChange={e=>setAddForm({...addForm,id:e.target.value})} style={{...inp,width:160}}/>
          <input placeholder="Provider" value={addForm.provider} onChange={e=>setAddForm({...addForm,provider:e.target.value})} style={{...inp,width:100}}/>
          <select value={addForm.tier} onChange={e=>setAddForm({...addForm,tier:e.target.value})} style={{...inp,width:80}}>
            <option value="">层级</option>{tiers.map(t=><option key={t}>{t}</option>)}
          </select>
          <input placeholder="上下文窗口" type="number" value={addForm.context_window} onChange={e=>setAddForm({...addForm,context_window:e.target.value})} style={{...inp,width:100}}/>
          <input placeholder="输入$/1k" type="number" step="0.001" value={addForm.cost_1k_input} onChange={e=>setAddForm({...addForm,cost_1k_input:e.target.value})} style={{...inp,width:90}}/>
          <input placeholder="输出$/1k" type="number" step="0.001" value={addForm.cost_1k_output} onChange={e=>setAddForm({...addForm,cost_1k_output:e.target.value})} style={{...inp,width:90}}/>
          <button onClick={handleAdd} style={{background:'var(--accent-green)',color:'#fff',border:'none',borderRadius:4,padding:'6px 12px',cursor:'pointer',fontSize:12}}>添加</button>
          <button onClick={()=>setShowAdd(false)} style={{background:'none',border:'none',color:'var(--text-muted)',cursor:'pointer',padding:6}}><X size={14}/></button>
        </div>
      )}

      {/* Model table */}
      <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',overflow:'hidden'}}>
        <table style={{width:'100%',borderCollapse:'collapse'}}>
          <thead><tr style={{borderBottom:'1px solid var(--border)',fontSize:11,color:'var(--text-muted)',textAlign:'left'}}>
            <th style={{padding:'7px 10px',width:180}}>模型</th>
            <th style={{padding:'7px 10px',width:80}}>Provider</th>
            <th style={{padding:'7px 10px',width:60}}>层级</th>
            <th style={{padding:'7px 10px',width:90}}>上下文</th>
            <th style={{padding:'7px 10px',width:120}}>成本 (/1k)</th>
            <th style={{padding:'7px 10px',width:60}}></th>
          </tr></thead>
          <tbody>
            {filtered.map(m=>{
              const isEditing = editId === (m.id || m.model)
              return (
                <tr key={m.id||m.model} style={{borderBottom:'1px solid var(--border)',fontSize:12,background:isEditing?'var(--bg-tertiary)':'transparent'}}>
                  <td style={{padding:'7px 10px',fontFamily:'var(--font-mono)',fontSize:12}}>
                    {isEditing
                      ? <input value={editForm.id} onChange={e=>setEditForm({...editForm,id:e.target.value})} style={{...inp,width:150}}/>
                      : <span style={{display:'flex',alignItems:'center',gap:6}}><Cpu size={12} color='var(--accent)'/>{m.id||m.model}</span>}
                  </td>
                  <td style={{padding:'7px 10px',color:'var(--text-secondary)'}}>
                    {isEditing
                      ? <input value={editForm.provider} onChange={e=>setEditForm({...editForm,provider:e.target.value})} style={{...inp,width:80}}/>
                      : (m.provider||'-')}
                  </td>
                  <td style={{padding:'7px 10px'}}>
                    {isEditing
                      ? <select value={editForm.tier} onChange={e=>setEditForm({...editForm,tier:e.target.value})} style={{...inp,width:60}}><option value="">-</option>{tiers.map(t=><option key={t}>{t}</option>)}</select>
                      : (m.tier ? <span style={{background:'var(--bg-tertiary)',padding:'1px 6px',borderRadius:3,fontSize:10,fontWeight:600,color:m.tier==='D'?'#f0883e':m.tier==='E+'?'#a371f7':'#58a6ff'}}>{m.tier}</span> : '-')}
                  </td>
                  <td style={{padding:'7px 10px',color:'var(--text-secondary)',fontFamily:'var(--font-mono)'}}>
                    {isEditing
                      ? <input type="number" value={editForm.context_window} onChange={e=>setEditForm({...editForm,context_window:e.target.value})} style={{...inp,width:80}}/>
                      : (m.context_window ? m.context_window.toLocaleString() : '-')}
                  </td>
                  <td style={{padding:'7px 10px',color:'var(--text-secondary)',fontSize:10}}>
                    {isEditing
                      ? <div style={{display:'flex',gap:4}}><input type="number" step="0.001" value={editForm.cost_1k_input} onChange={e=>setEditForm({...editForm,cost_1k_input:e.target.value})} style={{...inp,width:70}} placeholder="in"/><input type="number" step="0.001" value={editForm.cost_1k_output} onChange={e=>setEditForm({...editForm,cost_1k_output:e.target.value})} style={{...inp,width:70}} placeholder="out"/></div>
                      : (m.cost_1k_input ? <>${m.cost_1k_input} / ${m.cost_1k_output}</> : '-')}
                  </td>
                  <td style={{padding:'7px 10px'}}>
                    <div style={{display:'flex',gap:2}}>
                      {isEditing ? (
                        <><button onClick={saveEdit} style={{...iconBtn,color:'var(--accent-green)'}}><Check size={14}/></button>
                          <button onClick={()=>setEditId(null)} style={iconBtn}><X size={14}/></button></>
                      ) : (
                        <><button onClick={()=>startEdit(m)} style={iconBtn}><Edit3 size={14}/></button>
                          <button onClick={()=>api.deleteModel(m.id||m.model).then(fetch)} style={{...iconBtn,color:'var(--accent-red)'}}><Trash2 size={14}/></button></>
                      )}
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

const inp = { background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:3,padding:'3px 6px',color:'var(--text-primary)',fontSize:11,width:'100%' } as const
const iconBtn = { background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2 } as const
const filterBtn = (active: boolean) => ({ background:active?'var(--accent)':'var(--bg-tertiary)',color:active?'#fff':'var(--text-secondary)',border:'1px solid var(--border)',borderRadius:4,padding:'3px 10px',cursor:'pointer',fontSize:11 })
