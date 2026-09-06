import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { useSSE } from '../lib/useSSE'
import { useToast } from '../components/Toast'
import { Plus, RefreshCw, ChevronDown, ChevronRight } from 'lucide-react'

const PHASE_CN: Record<string,string> = {
  template:'模板', researching:'调研', gate1:'G1确认', planning:'规划', gate2:'G2确认',
  executing:'执行', integrating:'集成', reviewing:'审查', fixing:'修复', gate3:'G3确认', delivering:'交付', done:'完成'
}
const PC: Record<string,string> = {
  template:'var(--text-muted)', researching:'var(--accent)', gate1:'var(--accent-yellow)', planning:'var(--accent-purple)',
  gate2:'var(--accent-yellow)', executing:'var(--accent-green)', integrating:'var(--accent-green)',
  reviewing:'#ea580c', fixing:'var(--accent-red)', gate3:'var(--accent-yellow)', delivering:'var(--accent)', done:'var(--accent-green)'
}

function ReportBlock({ data }: { data: any }) {
  if (!data) return null
  const products = data.competitive_analysis?.products || []
  const pitfalls = data.pitfalls || []
  return (
    <div style={{ background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', padding: '8px 10px', marginBottom: 8 }}>
      <div className="fw-600 fs-11" style={{ marginBottom: 4 }}>📋 调研报告</div>
      {data.recommendation && <div className="fs-11" style={{ marginBottom: 4 }}><b>推荐方案：</b>{data.recommendation}</div>}
      {products.length > 0 && (
        <div className="fs-11" style={{ marginBottom: 4 }}>
          <b>竞品分析：</b>
          {products.map((x: any, i: number) => <div key={i}>· {x.name}（{x.type}）：{x.strengths}</div>)}
        </div>
      )}
      {pitfalls.length > 0 && <div className="fs-11"><b>关键坑：</b>{pitfalls.join('；')}</div>}
    </div>
  )
}

function ArchBlock({ data }: { data: any }) {
  if (!data) return null
  const modules = data.modules || []
  const tasks = data.tasks || []
  return (
    <div style={{ background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', padding: '8px 10px', marginBottom: 8 }}>
      <div className="fw-600 fs-11" style={{ marginBottom: 4 }}>🏗 架构方案</div>
      {data.architecture && <div className="fs-11" style={{ marginBottom: 4 }}>{data.architecture}</div>}
      {modules.length > 0 && (
        <div className="fs-11" style={{ marginBottom: 4 }}>
          <b>模块（{modules.length}）：</b>
          {modules.map((m: any, i: number) => <div key={i}>· {m.name} — {m.responsibility}</div>)}
        </div>
      )}
      {tasks.length > 0 && (
        <div className="fs-11">
          <b>任务（{tasks.length}）：</b>
          {tasks.map((t: any, i: number) => <div key={i}>· {t.id} {t.title}</div>)}
        </div>
      )}
    </div>
  )
}

export default function Projects() {
  const [projects, setProjects] = useState<any[]>([])
  const [expanded, setExpanded] = useState<string|null>(null)
  const [detail, setDetail] = useState<any>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [form, setForm] = useState({ name: '', description: '', template: 'feature' })
  const [loading, setLoading] = useState(true)
  const toast = useToast(s => s.add)

  const fetch = async () => {
    setLoading(true)
    try { const d: any = await api.projects(); setProjects(Array.isArray(d)?d:(d?.projects||[])) } catch { toast('加载项目失败', 'error') }
    setLoading(false)
  }
  useEffect(() => { fetch() }, [])
  useSSE(() => { fetch() })

  const toggle = async (id: string) => {
    if (expanded === id) { setExpanded(null); setDetail(null); return }
    setExpanded(id)
    try { const d = await api.project(id); setDetail(d) } catch { toast('加载项目详情失败', 'error') }
  }

  const create = async () => {
    if (!form.name) return
    await api.createProject(form)
    setShowCreate(false); setForm({ name: '', description: '', template: 'feature' }); fetch()
  }

  const phases = ['template','researching','gate1','planning','gate2','executing','integrating','reviewing','fixing','gate3','delivering','done']

  return (
    <div className="page-wrap-wide">
      <div className="flex-center gap-8" style={{ marginBottom: 12 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600 }}>项目</h2>
        <span className="fs-11 text-muted">{projects.length} 个</span>
        <span className="flex-1"/>
        <button onClick={fetch} className="btn-icon"><RefreshCw size={14}/></button>
        <button onClick={()=>setShowCreate(!showCreate)} className="btn-green" style={{ padding: '6px 12px', fontSize: 12, gap: 4 }}><Plus size={14}/> 新建</button>
      </div>

      {showCreate && (
        <div className="flex-center gap-8 flex-wrap" style={{ marginBottom: 10, padding: 10, background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
          <input value={form.name} onChange={e=>setForm({...form,name:e.target.value})} placeholder="项目名称" className="inp-sm" style={{ flex: 1 }}/>
          <input value={form.description} onChange={e=>setForm({...form,description:e.target.value})} placeholder="需求描述" className="inp-sm" style={{ flex: 2 }}/>
          <select value={form.template} onChange={e=>setForm({...form,template:e.target.value})} className="inp-sm" style={{ width: 'auto' }}>
            <option value="feature">新功能</option><option value="bugfix">Bug修复</option><option value="refactor">重构</option><option value="test">写测试</option><option value="review">代码审查</option>
          </select>
          <button onClick={create} className="btn-green" style={{ padding: '6px 14px', fontSize: 12 }}>创建</button>
        </div>
      )}

      {loading ? (
        <div>
          {[1,2,3].map(i => <div key={i} className="skeleton skeleton-row"/>)}
        </div>
      ) : (
        <>
          {projects.map((p: any) => (
            <div key={p.id} style={{ marginBottom: 6 }}>
              <div onClick={()=>toggle(p.id)}
                className="flex-center gap-8" style={{ padding: '8px 10px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', cursor: 'pointer' }}>
                <span className="text-muted">{expanded===p.id?<ChevronDown size={12}/>:<ChevronRight size={12}/>}</span>
                <span className="fw-600 fs-13 flex-1">{p.name}</span>
                <span className="fs-10 fw-600" style={{ color: PC[p.phase]||'var(--text-muted)' }}>{PHASE_CN[p.phase]||p.phase}</span>
                <span className="fs-10 text-muted">{p.task_count||0} 任务</span>
              </div>
              {expanded === p.id && detail && (
                <div style={{ marginLeft: 20, padding: '8px 14px', borderLeft: '1px solid var(--border)', fontSize: 12 }}>
                  <div className="flex-center gap-4 flex-wrap" style={{ marginBottom: 8 }}>
                    {phases.map(ph => (
                      <span key={ph} style={{
                        padding: '2px 6px', borderRadius: 3, fontSize: 10, fontWeight: ph===detail.phase?700:400,
                        color: ph===detail.phase?'#fff':(PC[ph]||'var(--text-muted)'),
                        background: ph===detail.phase?PC[ph]:'transparent',
                        border: '1px solid '+(PC[ph]||'var(--border)')
                      }}>{PHASE_CN[ph]||ph}</span>
                    ))}
                  </div>
                  <div className="text-secondary" style={{ marginBottom: 4 }}>{detail.description}</div>
                  {detail.repo_dir && <div className="fs-10 text-muted" style={{ marginBottom: 4 }}>📁 成品：{detail.repo_dir}</div>}
                  {detail.research_report && <ReportBlock data={detail.research_report} />}
                  {detail.architecture && <ArchBlock data={detail.architecture} />}
                  {detail.phase && detail.phase.startsWith('gate') && (
                    <div className="fs-10 text-muted" style={{ marginBottom: 8, color: 'var(--accent-yellow)' }}>
                      🛑 {PHASE_CN[detail.phase]} — 到「对话」里审批
                    </div>
                  )}
                  {detail.lineage && detail.lineage.length > 0 && (
                    <div className="fs-10 text-muted" style={{ marginTop: 4 }}>
                      {detail.lineage.slice(-5).map((l:any,i:number) => (
                        <div key={i}>[{l.action}] {l.agent||''} {l.task_count?l.task_count+'任务':''}</div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
          {!loading && projects.length === 0 && (
            <div className="fs-11 text-muted" style={{ padding: 20, textAlign: 'center' }}>暂无项目，点"新建"创建</div>
          )}
        </>
      )}
    </div>
  )
}
