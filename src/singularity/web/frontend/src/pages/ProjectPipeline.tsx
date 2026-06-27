import { useState, useEffect } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../lib/api'
import { Play } from 'lucide-react'

const PHASES = [
  { key: 'TEMPLATE', name: '模板', desc: '填写需求' },
  { key: 'RESEARCHING', name: '调研', desc: '市场/技术调研' },
  { key: 'GATE1', name: 'GATE1', desc: '人工确认PRD' },
  { key: 'PLANNING', name: '架构', desc: '系统/AI/前端架构' },
  { key: 'GATE2', name: 'GATE2', desc: '人工确认架构' },
  { key: 'EXECUTING', name: '实现', desc: '4工程师并行' },
  { key: 'GATE3', name: 'GATE3', desc: '人工确认交付' },
]

export default function ProjectPipeline() {
  const { id } = useParams<{id:string}>()
  const [project, setProject] = useState<any>(null)
  useEffect(() => { if(id) api.project(id).then(setProject) }, [id])

  if (!project) return <div style={{color:'var(--text-muted)'}}>加载中...</div>

  const currentPhase = project.phase

  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:4}}>{project.name || '未命名项目'}</h2>
      <div style={{fontSize:12,color:'var(--text-muted)',marginBottom:14,fontFamily:'var(--font-mono)'}}>ID: {project.id}</div>

      <div style={{display:'flex',gap:6,overflow:'auto',paddingBottom:8}}>
        {PHASES.map((p,i)=>(
          <div key={p.key} style={{
            minWidth:140, background:'var(--bg-secondary)',
            border:`2px solid ${currentPhase===p.key?'var(--accent)':PHASES.slice(0,i).map(x=>x.key).includes(currentPhase)?'var(--accent-green)':'var(--border)'}`,
            borderRadius:'var(--radius)',padding:12,opacity:currentPhase===p.key?1:0.7,
          }}>
            <div style={{fontWeight:700,fontSize:13,marginBottom:2}}>{p.name}</div>
            <div style={{fontSize:10,color:'var(--text-muted)'}}>{p.desc}</div>
            {currentPhase === p.key && <div style={{fontSize:10,color:'var(--accent)',marginTop:4}}>← 当前阶段</div>}
          </div>
        ))}
      </div>

      {/* Actions */}
      <div style={{marginTop:14,display:'flex',gap:6}}>
        {project.phase === 'TEMPLATE' && (
          <button onClick={()=>api.runPhase(project.id).then(()=>api.project(id!).then(setProject))}
            style={{background:'var(--accent)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'8px 16px',cursor:'pointer',fontSize:13,display:'flex',alignItems:'center',gap:6}}>
            <Play size={14}/> 启动流水线</button>
        )}
      </div>

      {/* Info */}
      <div style={{marginTop:16,background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
        <div style={{fontSize:13,fontWeight:600,marginBottom:6}}>项目详情</div>
        <div style={{fontSize:12,color:'var(--text-secondary)',lineHeight:1.8}}>
          <div>描述: {project.description || '-'}</div>
          <div>范围: {project.scope || '-'}</div>
          <div>模板: {project.template || '-'}</div>
          <div>阶段: {project.phase}</div>
          <div>任务数: {project.task_ids?.length || 0}</div>
          {project.architecture && <div>
            架构方案: 已产出 ({project.architecture.tasks?.length || 0} 个任务)
            {project.architecture.fusion_notes && (
              <span style={{marginLeft:8,fontSize:10,color:'var(--accent-purple)',fontWeight:600}}>
                多模型碰撞 · 解决{project.architecture.fusion_notes.resolved_contradictions||0}个矛盾
              </span>
            )}
          </div>}
        </div>
      </div>
    </div>
  )
}
