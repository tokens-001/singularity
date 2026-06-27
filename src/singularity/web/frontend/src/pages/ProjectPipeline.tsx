import { useState, useEffect } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../lib/api'
import { Play, GitCommit } from 'lucide-react'

const PHASES = [
  { key: 'TEMPLATE', name: '模板', desc: '填写需求' },
  { key: 'RESEARCHING', name: '调研', desc: '市场/技术调研' },
  { key: 'GATE1', name: '确认PRD', desc: '人工确认' },
  { key: 'PLANNING', name: '架构', desc: '系统/AI/前端架构' },
  { key: 'GATE2', name: '确认架构', desc: '人工确认' },
  { key: 'EXECUTING', name: '实现', desc: '4工程师并行' },
  { key: 'GATE3', name: '确认交付', desc: '人工确认' },
]

export default function ProjectPipeline() {
  const { id } = useParams<{id:string}>()
  const [project, setProject] = useState<any>(null)
  const [lineage, setLineage] = useState<any[]>([])

  useEffect(() => {
    if (!id) return
    Promise.all([api.project(id), api.gateConfirm(id,'','')]).then(([p]) => setProject(p)).catch(()=>{})
    // Fetch lineage separately
    fetch(`/api/projects/${id}/lineage`).then(r=>r.json()).then(setLineage).catch(()=>{})
  }, [id])

  if (!project) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  const currentPhase = project.phase

  return (
    <div style={{display:'flex',gap:14,height:'calc(100vh - 80px)'}}>
      {/* Left: Pipeline + Info */}
      <div style={{flex:1,overflow:'auto'}}>
        <h2 style={{fontSize:16,fontWeight:600,marginBottom:4}}>{project.name || '未命名项目'}</h2>
        <div style={{fontSize:11,color:'var(--text-muted)',marginBottom:14,fontFamily:'var(--font-mono)'}}>ID: {project.id}</div>

        <div style={{display:'flex',gap:6,overflow:'auto',paddingBottom:8,marginBottom:14}}>
          {PHASES.map((p,i)=>(
            <div key={p.key} style={{
              minWidth:110, background:'var(--bg-secondary)',
              border:`2px solid ${currentPhase===p.key?'var(--accent)':PHASES.slice(0,i).map(x=>x.key).includes(currentPhase)?'var(--accent-green)':'var(--border)'}`,
              borderRadius:'var(--radius)',padding:10,opacity:currentPhase===p.key?1:0.7,fontSize:11,
            }}>
              <div style={{fontWeight:700,fontSize:12}}>{p.name}</div>
              <div style={{fontSize:9,color:'var(--text-muted)'}}>{p.desc}</div>
              {currentPhase===p.key&&<div style={{fontSize:9,color:'var(--accent)',marginTop:2}}>← 当前</div>}
            </div>
          ))}
        </div>

        {project.phase==='TEMPLATE'&&(
          <button onClick={()=>api.runPhase(project.id).then(()=>api.project(id!).then(setProject))}
            style={{background:'var(--accent)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'8px 16px',cursor:'pointer',fontSize:13,display:'flex',alignItems:'center',gap:6,marginBottom:14}}>
            <Play size={14}/> 启动流水线</button>
        )}

        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
          <div style={{fontSize:13,fontWeight:600,marginBottom:8}}>项目详情</div>
          <div style={{fontSize:12,color:'var(--text-secondary)',lineHeight:1.8}}>
            <div>描述: {project.description || '-'}</div>
            <div>范围: {project.scope || '-'}</div>
            <div>阶段: {project.phase}</div>
            <div>任务数: {project.task_ids?.length || 0}</div>
            {project.architecture && <div>架构: 已产出 ({project.architecture.tasks?.length||0} 个任务)
              {project.architecture.fusion_notes&&<span style={{marginLeft:8,fontSize:10,color:'var(--accent-purple)',fontWeight:600}}>多模型碰撞</span>}
            </div>}
          </div>
        </div>
      </div>

      {/* Right: Lineage / 决策链 */}
      <div style={{width:320,flexShrink:0,display:'flex',flexDirection:'column'}}>
        <div style={{display:'flex',alignItems:'center',gap:6,marginBottom:8}}>
          <GitCommit size={14} color='var(--text-secondary)'/>
          <span style={{fontSize:12,fontWeight:600,color:'var(--text-secondary)'}}>决策链</span>
          <span style={{fontSize:10,color:'var(--text-muted)'}}>({lineage.length} 步)</span>
        </div>
        <div style={{flex:1,background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:8,overflow:'auto',fontSize:10,lineHeight:1.8}}>
          {lineage.length===0 && <span style={{color:'var(--text-muted)'}}>暂无决策记录</span>}
          {lineage.map((l:any,i:number)=><div key={i} style={{borderBottom:'1px solid var(--border)',padding:'4px 0',marginBottom:2}}>
            <span style={{color:'var(--accent)',fontWeight:600}}>{l.action||l.event||'?'}</span>
            <div style={{color:'var(--text-muted)',fontSize:9}}>
              {l.agent && <span>模型: {l.agent} · </span>}
              {l.task_count && <span>{l.task_count} 任务 · </span>}
              {l.traceability_items && <span>{l.traceability_items} 追溯 · </span>}
              {l.validation_issues && <span style={{color:'var(--accent-red)'}}>{l.validation_issues} 问题</span>}
            </div>
          </div>)}
        </div>
      </div>
    </div>
  )
}
