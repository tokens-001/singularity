import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Settings2, Server, Key, Plus, Trash2, RefreshCw, FlaskConical, Save } from 'lucide-react'

type Tab = 'apistore' | 'fusion' | 'mcp'

export default function Settings() {
  const [tab, setTab] = useState<Tab>('apistore')
  const [apiStore, setApiStore] = useState<any[]>([])
  const [mcpServers, setMcpServers] = useState<any[]>([])
  const [mcpTools, setMcpTools] = useState<any[]>([])
  const [fusion, setFusion] = useState<any>(null)
  const [agents, setAgents] = useState<any>({})
  const [showApiForm, setShowApiForm] = useState(false)
  const [apiForm, setApiForm] = useState({id:'',base_url:'',api_key_env:'',provider:''})

  // Fusion state
  const [fusionTier, setFusionTier] = useState('triple')
  const [fusionModels, setFusionModels] = useState<string[]>([])
  const [fusionJudge, setFusionJudge] = useState('')
  const [fusionCall, setFusionCall] = useState('')
  const [fusionAuto, setFusionAuto] = useState(true)
  const [fusionSuper, setFusionSuper] = useState({redteam:'2',iters:'3',human:true,perspectives:'架构/安全/性能/可维护性'})
  const [fusionStatus, setFusionStatus] = useState('')

  const refresh = () => { api.apiStore().then(setApiStore) }
  useEffect(() => {
    refresh(); api.mcpServers().then(setMcpServers); api.mcpTools().then(setMcpTools)
    api.fusionConfig().then(cfg => {
      setFusion(cfg)
      // Load current tier config
      const tc = cfg?.triple || cfg?.dual || {}
      setFusionModels(tc.models || [])
      setFusionJudge(tc.judge_model || '')
      setFusionCall(tc.call_model || '')
    })
    api.agents().then(setAgents)
  }, [])

  useEffect(() => {
    if (!fusion || !fusionTier) return
    const tc = fusion[fusionTier] || {}
    setFusionModels(tc.models || [])
    setFusionJudge(tc.judge_model || '')
    setFusionCall(tc.call_model || '')
  }, [fusionTier, fusion])

  const toggleModel = (m: string) => {
    setFusionModels(prev => prev.includes(m) ? prev.filter(x=>x!==m) : [...prev, m])
  }

  const allModels = () => {
    const models: string[] = []
    for (const lvl of ['D','E+','E']) for (const a of (agents?.[lvl]||[])) if (!models.includes(a.model)) models.push(a.model)
    return models
  }

  const saveFusion = async () => {
    const cfg = {
      ...fusion,
      [fusionTier]: { ...(fusion?.[fusionTier]||{}), models: fusionModels, judge_model: fusionJudge, call_model: fusionCall },
      auto: { ...(fusion?.auto||{}), trigger_types: fusionAuto ? ['fusion','refactor'] : [] }
    }
    if (fusionTier === 'super') cfg[fusionTier] = { ...cfg[fusionTier], detail: fusionSuper }
    await api.updateFusionConfig(cfg)
    setFusionStatus('已保存')
    setTimeout(()=>setFusionStatus(''), 2000)
    setFusion(cfg)
  }

  const testFusion = async () => {
    setFusionStatus('测试中...')
    try {
      await api.updateFusionConfig({...fusion, [fusionTier]:{models:fusionModels,judge_model:fusionJudge,call_model:fusionCall}})
      setFusionStatus('配置已更新，创建项目测试')
    } catch { setFusionStatus('失败') }
  }

  const addApi = async () => { if(!apiForm.id||!apiForm.base_url)return;await api.addApiStore(apiForm);setApiForm({id:'',base_url:'',api_key_env:'',provider:''});setShowApiForm(false);refresh() }

  const tabs = [
    { key: 'apistore' as Tab, label: 'API 连接', icon: Key },
    { key: 'fusion' as Tab, label: '多模型融合', icon: FlaskConical },
    { key: 'mcp' as Tab, label: 'MCP 工具', icon: Server },
  ]

  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:14}}>系统配置</h2>
      <div style={{display:'flex',gap:6,marginBottom:14}}>
        {tabs.map(t=>(<button key={t.key} onClick={()=>setTab(t.key)} style={{background:tab===t.key?'var(--accent)':'var(--bg-secondary)',color:tab===t.key?'#fff':'var(--text-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:'6px 14px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:4}}><t.icon size={14}/>{t.label}</button>))}
      </div>

      {/* ═══ API Store ═══ */}
      {tab==='apistore'&&(
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
          <div style={{display:'flex',alignItems:'center',marginBottom:10}}>
            <span style={{fontSize:13,fontWeight:600}}>API 连接 ({apiStore.length})</span>
            <button onClick={()=>setShowApiForm(!showApiForm)} style={{marginLeft:'auto',background:'var(--accent)',color:'#fff',border:'none',borderRadius:4,padding:'4px 10px',cursor:'pointer',fontSize:11,display:'flex',alignItems:'center',gap:3}}><Plus size={12}/> 添加</button>
          </div>
          {showApiForm&&(
            <div style={{display:'flex',gap:8,marginBottom:10,flexWrap:'wrap',alignItems:'center',padding:10,background:'var(--bg-tertiary)',borderRadius:4}}>
              <input placeholder="ID" value={apiForm.id} onChange={e=>setApiForm({...apiForm,id:e.target.value})} style={{...inp,width:100}}/>
              <input placeholder="Base URL" value={apiForm.base_url} onChange={e=>setApiForm({...apiForm,base_url:e.target.value})} style={{...inp,width:220}}/>
              <input placeholder="环境变量" value={apiForm.api_key_env} onChange={e=>setApiForm({...apiForm,api_key_env:e.target.value})} style={{...inp,width:130}}/>
              <input placeholder="Provider" value={apiForm.provider} onChange={e=>setApiForm({...apiForm,provider:e.target.value})} style={{...inp,width:100}}/>
              <button onClick={addApi} style={btn('var(--accent-green)')}>添加</button>
              <button onClick={()=>setShowApiForm(false)} style={{background:'none',border:'none',color:'var(--text-muted)',cursor:'pointer',fontSize:16}}>✕</button>
            </div>
          )}
          {apiStore.map((a:any)=>(<div key={a.id} style={{display:'flex',justifyContent:'space-between',alignItems:'center',padding:'8px 10px',background:'var(--bg-tertiary)',borderRadius:4,marginBottom:4}}>
            <div><span style={{fontWeight:600,fontSize:13}}>{a.provider||a.id}</span><span style={{fontSize:11,color:'var(--text-secondary)',marginLeft:8}}>{a.base_url?.slice(0,50)}</span></div>
            <div style={{display:'flex',gap:4,alignItems:'center'}}><span style={{fontSize:10,color:a.status==='active'?'var(--accent-green)':'var(--text-muted)'}}>{a.status||'?'}</span><button onClick={()=>api.scanApiStore(a.id).then(refresh)} style={{...iconBtn}}><RefreshCw size={12}/></button><button onClick={()=>api.deleteApiStore(a.id).then(refresh)} style={{...iconBtn,color:'var(--accent-red)'}}><Trash2 size={12}/></button></div>
          </div>))}
        </div>
      )}

      {/* ═══ Fusion 多模型融合 ═══ */}
      {tab==='fusion'&&(
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
          <div style={{fontSize:13,fontWeight:600,marginBottom:10}}>多模型融合 — 并行派发→五维分析→定稿</div>

          {/* Tier selector */}
          <div style={{display:'flex',alignItems:'center',gap:8,marginBottom:10}}>
            <span style={{fontSize:11,color:'var(--text-secondary)',whiteSpace:'nowrap'}}>框架:</span>
            <select value={fusionTier} onChange={e=>setFusionTier(e.target.value)} style={inp}>
              <option value="dual">双模型 — 2路并行+合成</option>
              <option value="triple">三模型 — 3路并行+合成</option>
              <option value="super">超级协作 — N模型×多视角×对抗验证</option>
            </select>
          </div>

          {/* Model checkboxes */}
          <div style={{marginBottom:10}}>
            <div style={{fontSize:10,color:'var(--text-muted)',marginBottom:4}}>参与模型:</div>
            <div style={{display:'flex',flexWrap:'wrap',gap:6}}>
              {allModels().map(m=>(<label key={m} style={{display:'flex',alignItems:'center',gap:4,fontSize:11,cursor:'pointer',padding:'3px 8px',background:fusionModels.includes(m)?'var(--accent)':'var(--bg-tertiary)',color:fusionModels.includes(m)?'#fff':'var(--text-secondary)',borderRadius:4}}>
                <input type="checkbox" checked={fusionModels.includes(m)} onChange={()=>toggleModel(m)} style={{display:'none'}}/>{m}</label>))}
            </div>
          </div>

          {/* Judge + Call */}
          <div style={{display:'flex',alignItems:'center',gap:8,marginBottom:10}}>
            <span style={{fontSize:10,color:'var(--text-muted)'}}>裁判:</span>
            <select value={fusionJudge} onChange={e=>setFusionJudge(e.target.value)} style={{...inp,width:130}}>
              <option value="">选裁判模型</option>{allModels().map(m=><option key={m}>{m}</option>)}</select>
            <span style={{fontSize:10,color:'var(--text-muted)'}}>定稿:</span>
            <select value={fusionCall} onChange={e=>setFusionCall(e.target.value)} style={{...inp,width:130}}>
              <option value="">选定稿模型</option>{allModels().map(m=><option key={m}>{m}</option>)}</select>
          </div>

          {/* Super mode options */}
          {fusionTier==='super'&&(
            <div style={{marginBottom:10,fontSize:10,color:'var(--text-secondary)',padding:8,background:'var(--bg-tertiary)',borderRadius:4}}>
              <div>多视角: {fusionSuper.perspectives}</div>
              <div>对抗验证: {fusionSuper.redteam} 个 red-team · 迭代上限: {fusionSuper.iters} 轮</div>
              <label style={{marginTop:4,display:'flex',alignItems:'center',gap:4}}><input type="checkbox" checked={fusionSuper.human} onChange={e=>setFusionSuper({...fusionSuper,human:e.target.checked})}/>人工卡点</label>
            </div>
          )}

          <div style={{fontSize:10,color:'var(--text-muted)',marginBottom:8}}>两阶段合成: 裁判五维JSON → 调用模型定稿</div>

          {/* Auto trigger */}
          <label style={{display:'flex',alignItems:'center',gap:6,marginBottom:10,fontSize:10,color:'var(--text-secondary)'}}>
            <input type="checkbox" checked={fusionAuto} onChange={e=>setFusionAuto(e.target.checked)}/>自动触发 (架构/安全/跨模块任务)
          </label>

          {/* Buttons */}
          <div style={{display:'flex',alignItems:'center',gap:8}}>
            <button onClick={testFusion} style={{...btn('var(--accent-purple)'),display:'flex',alignItems:'center',gap:4}}><FlaskConical size={14}/>测试碰撞</button>
            <button onClick={saveFusion} style={{...btn('var(--accent-green)'),display:'flex',alignItems:'center',gap:4}}><Save size={14}/>保存配置</button>
            <span style={{fontSize:10,color:fusionStatus.includes('失败')?'var(--accent-red)':'var(--accent-green)'}}>{fusionStatus}</span>
          </div>
        </div>
      )}

      {/* ═══ MCP ═══ */}
      {tab==='mcp'&&(
        <div>
          <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14,marginBottom:14}}>
            <div style={{fontSize:13,fontWeight:600,marginBottom:8}}>MCP 服务器 ({mcpServers.length})</div>
            {mcpServers.map((s:any)=>(<div key={s.name} style={{display:'flex',justifyContent:'space-between',alignItems:'center',padding:'6px 10px',background:'var(--bg-tertiary)',borderRadius:4,marginBottom:4}}>
              <div><span style={{fontWeight:600,fontSize:13}}>{s.name}</span><span style={{fontSize:10,color:'var(--text-muted)',marginLeft:8}}>{s.url}</span></div>
              <span style={{display:'flex',alignItems:'center',gap:4,fontSize:11,color:s.status==='connected'?'var(--accent-green)':'var(--text-muted)'}}><span style={{width:6,height:6,borderRadius:'50%',background:s.status==='connected'?'var(--accent-green)':'var(--text-muted)',display:'inline-block'}}/>{s.status}</span>
            </div>))}
          </div>
          <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
            <div style={{fontSize:13,fontWeight:600,marginBottom:8}}>MCP 工具 ({mcpTools.length})</div>
            <table style={{width:'100%',borderCollapse:'collapse'}}><thead><tr style={{borderBottom:'1px solid var(--border)',fontSize:11,color:'var(--text-muted)',textAlign:'left'}}><th style={{padding:'6px 10px'}}>工具</th><th style={{padding:'6px 10px',width:100}}>服务器</th><th style={{padding:'6px 10px'}}>描述</th></tr></thead><tbody>
              {mcpTools.map((t:any,i:number)=>(<tr key={i} style={{borderBottom:'1px solid var(--border)',fontSize:12}}><td style={{padding:'6px 10px',fontFamily:'var(--font-mono)',fontSize:11}}>{t.name}</td><td style={{padding:'6px 10px',color:'var(--text-secondary)'}}>{t.server}</td><td style={{padding:'6px 10px',color:'var(--text-muted)',fontSize:11}}>{t.description?.slice(0,80)}</td></tr>))}
            </tbody></table>
          </div>
        </div>
      )}
    </div>
  )
}

const inp = { background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:3,padding:'5px 8px',color:'var(--text-primary)',fontSize:12 } as const
const btn = (bg:string) => ({ background:bg, color:'#fff', border:'none', borderRadius:4, padding:'6px 14px', cursor:'pointer', fontSize:12 })
const iconBtn = { background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2 } as const
