import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Cpu, Trash2 } from 'lucide-react'

export default function ModelManagement() {
  const [models, setModels] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  useEffect(() => { api.models().then(setModels).finally(()=>setLoading(false)) }, [])

  if (loading) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:14}}>模型注册表 ({models.length})</h2>
      <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(280px,1fr))',gap:10}}>
        {models.map(m=>(
          <div key={m.id||m.model} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
            <div style={{display:'flex',justifyContent:'space-between',alignItems:'start'}}>
              <div>
                <div style={{fontWeight:600,fontFamily:'var(--font-mono)',fontSize:13,display:'flex',alignItems:'center',gap:6}}>
                  <Cpu size={14} color='var(--accent)'/>{m.id||m.model}</div>
                <div style={{fontSize:11,color:'var(--text-secondary)',marginTop:2}}>{m.provider} · {m.tier}</div>
              </div>
            </div>
            {m.context_window && <div style={{fontSize:10,color:'var(--text-muted)',marginTop:4}}>上下文: {m.context_window}</div>}
            {m.cost_1k_input && <div style={{fontSize:10,color:'var(--text-muted)'}}>输入: ${m.cost_1k_input}/1k · 输出: ${m.cost_1k_output}/1k</div>}
          </div>
        ))}
      </div>
    </div>
  )
}
