const AGENTS = [{name:'kimi-k2.7-code',level:'E',status:'active',tasks:12},{name:'qwen3-coder-plus',level:'E+',status:'active',tasks:8},{name:'glm-5.2',level:'E+',status:'idle',tasks:0},{name:'gpt-5.5',level:'D',status:'active',tasks:3}]
export default function AgentDashboard() {
  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:16}}>Agent 管理</h2>
      <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(240px,1fr))',gap:12}}>
        {AGENTS.map(a=>(
          <div key={a.name} style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
            <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:8}}>
              <span style={{fontWeight:600,fontFamily:'var(--font-mono)',fontSize:13}}>{a.name}</span>
              <span style={{background:a.status==='active'?'var(--accent-green)':'var(--text-muted)',width:8,height:8,borderRadius:'50%',display:'inline-block'}}/>
            </div>
            <div style={{display:'flex',gap:8,fontSize:11,color:'var(--text-secondary)'}}>
              <span style={{background:'var(--bg-tertiary)',padding:'1px 6px',borderRadius:3}}>{a.level}</span>
              <span>{a.tasks} 任务</span>
              <span>{a.status}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
