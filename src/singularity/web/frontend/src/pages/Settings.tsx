import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Settings2, Server, Key, Plus, Trash2, RefreshCw, Wrench } from 'lucide-react'

type Tab = 'apistore' | 'mcp' | 'fusion'

export default function Settings() {
  const [tab, setTab] = useState<Tab>('apistore')
  const [apiStore, setApiStore] = useState<any[]>([])
  const [mcpServers, setMcpServers] = useState<any[]>([])
  const [mcpTools, setMcpTools] = useState<any[]>([])
  const [fusion, setFusion] = useState<any>(null)
  const [showApiForm, setShowApiForm] = useState(false)
  const [apiForm, setApiForm] = useState({id:'',base_url:'',api_key_env:'',provider:''})

  const refresh = () => { api.apiStore().then(setApiStore) }
  useEffect(() => { refresh(); api.mcpServers().then(setMcpServers); api.mcpTools().then(setMcpTools); api.fusionConfig().then(setFusion) }, [])

  const addApi = async () => { if(!apiForm.id||!apiForm.base_url)return;await api.addApiStore(apiForm);setApiForm({id:'',base_url:'',api_key_env:'',provider:''});setShowApiForm(false);refresh() }

  const tabs = [
    { key: 'apistore' as Tab, label: 'API 连接', icon: Key, desc: '管理 API 密钥和扫描模型' },
    { key: 'mcp' as Tab, label: 'MCP 工具', icon: Server, desc: 'MCP 服务器和工具' },
    { key: 'fusion' as Tab, label: 'Fusion', icon: Settings2, desc: '多模型碰撞配置' },
  ]

  return (
    <div>
      <h2 style={{fontSize:16,fontWeight:600,marginBottom:14}}>系统配置</h2>
      <div style={{display:'flex',gap:6,marginBottom:14}}>
        {tabs.map(t=>(<button key={t.key} onClick={()=>setTab(t.key)} style={{background:tab===t.key?'var(--accent)':'var(--bg-secondary)',color:tab===t.key?'#fff':'var(--text-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:'6px 12px',cursor:'pointer',fontSize:12,display:'flex',flexDirection:'column',alignItems:'flex-start',gap:2}}><div style={{display:'flex',alignItems:'center',gap:4}}><t.icon size={14}/>{t.label}</div><span style={{fontSize:9,opacity:0.7}}>{t.desc}</span></button>))}
      </div>

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
            <div style={{display:'flex',gap:4,alignItems:'center'}}>
              <span style={{fontSize:10,color:a.status==='active'?'var(--accent-green)':'var(--text-muted)'}}>{a.status||'?'}</span>
              <button onClick={()=>api.scanApiStore(a.id).then(refresh)} style={{...iconBtn}}><RefreshCw size={12}/></button>
              <button onClick={()=>api.deleteApiStore(a.id).then(refresh)} style={{...iconBtn,color:'var(--accent-red)'}}><Trash2 size={12}/></button>
            </div>
          </div>))}
        </div>
      )}

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

      {tab==='fusion'&&fusion&&(
        <div style={{background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:14}}>
          <div style={{fontSize:13,fontWeight:600,marginBottom:8}}>Fusion 配置</div>
          {Object.entries(fusion).map(([tier,cfg]:[string,any])=>(<div key={tier} style={{marginBottom:8,padding:8,background:'var(--bg-tertiary)',borderRadius:4}}>
            <div style={{fontSize:12,fontWeight:600,color:'var(--accent)'}}>{tier}</div>
            <pre style={{fontSize:10,color:'var(--text-secondary)',margin:0}}>{JSON.stringify(cfg,null,2)}</pre>
          </div>))}
        </div>
      )}
    </div>
  )
}

const inp = { background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:3,padding:'5px 8px',color:'var(--text-primary)',fontSize:12 } as const
const btn = (bg:string) => ({ background:bg, color:'#fff', border:'none', borderRadius:4, padding:'6px 14px', cursor:'pointer', fontSize:12 })
const iconBtn = { background:'none',border:'none',color:'var(--text-secondary)',cursor:'pointer',padding:2 } as const
