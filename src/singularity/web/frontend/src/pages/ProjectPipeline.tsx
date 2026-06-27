import { useParams } from 'react-router-dom'
export default function ProjectPipeline() {
  const { id } = useParams()
  const PHASES = [
    {name:'定义',status:'done',roles:'产品经理·交互·UI·研究员'},
    {name:'GATE1',status:'done',roles:'人工确认'},
    {name:'架构',status:'running',roles:'系统架构·AI架构·前端架构'},
    {name:'GATE2',status:'pending',roles:'人工确认'},
    {name:'实现',status:'pending',roles:'前端·后端·数据·DevOps'},
    {name:'验收',status:'pending',roles:'QA·安全审计'},
    {name:'GATE3',status:'pending',roles:'人工确认'},
  ]
  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:16}}>项目 {id} — 流水线</h2>
      <div style={{display:'flex',gap:8,overflow:'auto'}}>
        {PHASES.map((p,i)=>(
          <div key={p.name} style={{minWidth:160,background:'var(--bg-secondary)',border:`2px solid ${p.status==='running'?'var(--accent)':p.status==='done'?'var(--accent-green)':'var(--border)'}`,borderRadius:'var(--radius)',padding:14}}>
            <div style={{fontWeight:600,fontSize:14,marginBottom:4}}>{p.name}</div>
            <div style={{fontSize:11,color:'var(--text-secondary)',marginBottom:4}}>{p.roles}</div>
            <span style={{fontSize:11,color:p.status==='running'?'var(--accent)':'var(--text-muted)'}}>{p.status==='running'?'⏳ 进行中':p.status==='done'?'✓ 完成':'○ 待开始'}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
