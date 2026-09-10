import { useState, useEffect } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useAppStore } from '../stores/app'
import { useSSE, useSSEConnected } from '../lib/useSSE'
import { useToast } from '../lib/toast'
import { getPinned } from '../lib/pinned'
import { api } from '../lib/api'
import { fmtCost, isUnpriced } from '../lib/money'
import { MessageSquare, List, Settings, Boxes, Activity } from 'lucide-react'

const NAV = [
  { path: '/', label: '对话', icon: MessageSquare },
  { path: '/projects', label: '项目', icon: Boxes },
  { path: '/tasks', label: '任务', icon: List },
  { path: '/usage', label: '用量', icon: Activity },
  { path: '/config', label: '配置', icon: Settings },
]

const PHASE_CN: Record<string,string> = {
  template:'待开始', researching:'调研', gate1:'G1 审核', planning:'架构', gate2:'G2 审核',
  executing:'执行中', integrating:'集成', reviewing:'审查', fixing:'修复', gate3:'G3 审核', delivering:'交付', done:'完成'
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return (n/1_000_000).toFixed(1) + 'M'
  if (n >= 1_000) return (n/1_000).toFixed(1) + 'K'
  return String(n || 0)
}

/** 预算额度条 —— 抄 ZCode 侧边栏那个 "N% used / Remaining / Total"。
 *
 * **budget <= 0 时什么都不渲染**：没配预算就无所谓"百分之几"，
 * 退回 `0% used / 剩余 $0.00 / 总额 $0.00` 等于**编了一个用户从没设过的套餐**。
 *
 * 导出是为了能单独测这条规则 —— 它埋在 AppLayout 里时测不到（要 mock router + SSE + store）。
 */
export function BudgetMeter({ used, budget, unpriced }: {
  used: number; budget: number; unpriced: string[]
}) {
  if (!(budget > 0)) return null
  const pct = (used / budget) * 100
  const remain = budget - used            // 不夹到 0：超预算是真实状态
  const lowerBound = unpriced.length > 0
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 3, marginTop: 2 }}
      title={lowerBound ? `以下模型未配置单价，实际花费更高：${unpriced.join('、')}` : undefined}>
      <div className="flex-between" style={{ fontSize: 10, color: '#6b6b68' }}>
        <span>今日预算</span>
        {/* 有模型没配单价时读数是**下限** —— 必须看起来就是下限 */}
        <span style={{ color: lowerBound ? '#d97706' : undefined }}>
          {lowerBound ? '≥' : ''}{pct.toFixed(0)}% used
        </span>
      </div>
      <div style={{ height: 3, borderRadius: 2, background: '#f3f2ec' }}>
        {/* 夹的是**条**，不是数字 —— 140% 就得显示 140% */}
        <div style={{ height: 3, borderRadius: 2, width: `${Math.min(100, pct)}%`,
          background: pct >= 100 ? 'var(--accent-red, #dc2626)' : 'var(--accent, #2563eb)' }} />
      </div>
      <div className="flex-between" style={{ fontSize: 10, color: '#b5b2a8' }}>
        <span>剩余 ${remain.toFixed(2)}</span>
        <span>总额 ${budget.toFixed(2)}</span>
      </div>
      <div style={{ fontSize: 10, color: '#b5b2a8' }}>每日 00:00 重置</div>
    </div>
  )
}

export default function AppLayout() {
  const { sidebarCollapsed, toggleSidebar } = useAppStore()
  const setActiveProject = useAppStore(s => s.setActiveProject)
  const activePid = useAppStore(s => s.activeProjectId)
  const navigate = useNavigate()
  const location = useLocation()
  const pathname = location.pathname
  const [projects, setProjects] = useState<any[]>([])
  const [pinned, setPinned] = useState<string[]>(getPinned)
  const addToast = useToast()
  const sidebarWidth = sidebarCollapsed ? 0 : 260
  const [usage, setUsage] = useState<any>({})
  // 按模型那几行**默认展开**。折叠着的时候侧边栏只有一行"今日 N tokens"，
  // 看着跟"总量统计"没区别 —— 而用户要的正是按模型看谁在吃预算。
  const [showModels, setShowModels] = useState(true)
  const [loopRunning, setLoopRunning] = useState(false)
  const [conflicts, setConflicts] = useState<any[]>([])

  const sseAlive = useSSEConnected()

  const loadProjects = async () => {
    try { const d: any = await api.projects(); setProjects(Array.isArray(d)?d:(d?.projects||[])) } catch { addToast('加载项目失败', 'error') }
    setPinned(getPinned())   // 置顶在项目页改的，这里跟着重排
  }
  useEffect(() => {
    loadProjects()
    if (sseAlive) return   // SSE 活着 → 靠事件驱动；断了才退回轮询
    const t = setInterval(loadProjects, 10000); return () => clearInterval(t)
  }, [sseAlive])
  useSSE(loadProjects, { kinds: ['project', 'workflow', 'system', 'task'], debounceMs: 400 })

  // token 用量 / 调度状态 / 冲突 都没有 SSE 事件，只能轮询；30s 够用
  const refreshLoop = async () => {
    const s = await api.loopStatus().catch(() => null)
    if (s) setLoopRunning(!!(s as any).running)
  }
  useEffect(() => {
    const f = async () => {
      const [u, s, c] = await Promise.all([
        api.tokenUsage().catch(() => null),
        api.loopStatus().catch(() => null),
        api.conflicts().catch(() => null),
      ])
      if (u) setUsage(u)
      if (s) setLoopRunning(!!(s as any).running)
      if (c) setConflicts((c as any).conflicts || [])
    }
    f(); const t = setInterval(f, 30000); return () => clearInterval(t)
  }, [])

  // 旧后端不返 by_model（字段缺失 → null）时整行不可点、不显示箭头，
  // 免得箭头承诺了却展开出空
  const models: any[] = Array.isArray(usage?.by_model) ? usage.by_model : []
  // 今天用过但没配单价的模型 → 上面的总额只是下限，要标出来
  const unpriced: string[] = Array.isArray(usage?.unpriced_models) ? usage.unpriced_models : []
  const budget: number = usage?.budget_daily || 0

  const selectProject = (pid: string) => { setActiveProject(pid); navigate('/') }

  return (
    <div className="app-shell">
      <div className="sidebar" style={{ width: sidebarWidth }}>
        <div style={{ padding: '16px 14px 12px' }}>
          <span style={{ fontSize: 14, fontWeight: 700, color: '#141413', letterSpacing: 1.5 }}>SINGULARITY</span>
        </div>

        <div style={{ padding: '4px 8px', display: 'flex', flexDirection: 'column', gap: 1 }}>
          {NAV.map(n => {
            const active = pathname === n.path
            return (
              <button key={n.path} onClick={() => { if (n.path === '/') setActiveProject('_default'); navigate(n.path) }} className={active ? 'nav-item nav-active' : 'nav-item'}>
                <n.icon size={15}/> {n.label}
              </button>
            )
          })}
        </div>

        <div style={{ padding: '6px 12px 4px', fontSize: 10, color: '#9a9993', fontWeight: 600 }}>项目列表</div>
        <div style={{ flex: 1, overflow: 'auto', padding: '0 6px' }}>
          {[...projects].sort((a, b) => {
              const aPin = pinned.includes(a.id) ? 0 : 1
              const bPin = pinned.includes(b.id) ? 0 : 1
              return aPin - bPin || a.name.localeCompare(b.name)
            }).map((p: any) => {
              const isPinned = pinned.includes(p.id)
              const isActive = activePid === p.id
              return (
                <div key={p.id} onClick={() => selectProject(p.id)} role="button" tabIndex={0}
                  onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectProject(p.id) } }}
                  style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '5px 8px', margin: '1px 0',
                    borderRadius: 6, cursor: 'pointer', fontSize: 12, color: isActive ? '#141413' : '#6b6b68',
                    background: isActive ? '#f3f2ec' : 'transparent' }}>
                  <span style={{ color: isPinned ? '#d97706' : '#b5b2a8', fontSize: 10 }}>#</span>
                  <span className="truncate" style={{ flex: 1 }}>{p.name}</span>
                  <span className="fs-10" style={{ color: '#b5b2a8' }}>{PHASE_CN[p.phase] || p.phase}</span>
                </div>
              )
            })}
        </div>

        <div style={{ padding: '8px 12px', borderTop: '1px solid #f3f2ec', display: 'flex', flexDirection: 'column', gap: 3 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11 }}>
            <span style={{ color: loopRunning ? '#16a34a' : '#b5b2a8', fontSize: 8 }}>●</span>
            <span style={{ color: '#6b6b68' }}>调度{loopRunning ? '运行中' : '已停'}</span>
            {/* 原来只显示状态、**没有任何启停入口**（api.ts 的 startLoop/stopLoop 零调用）——
                侧边栏写着"运行中/已停"却没法操作，只能去敲 CLI。 */}
            <button
              className="btn-icon"
              style={{ fontSize: 11, padding: '0 5px' }}
              title={loopRunning ? '停止调度循环' : '启动调度循环'}
              onClick={async () => {
                try {
                  await (loopRunning ? api.stopLoop() : api.startLoop())
                } catch { /* 失败也无所谓，下面的重拉会显示真实状态 */ }
                setTimeout(refreshLoop, 400)
              }}
            >{loopRunning ? '停' : '启'}</button>
            {conflicts.length > 0 && (
              <span style={{ marginLeft: 'auto', color: '#dc2626' }}
                title={conflicts.map((c: any) => c.task_id || c.id || '').join(', ')}>⚠ {conflicts.length} 冲突</span>
            )}
          </div>
          <BudgetMeter used={usage?.daily_cost || 0} budget={budget} unpriced={unpriced} />
          {/* 总量只说明"花了多少"，回答不了"该换谁" —— 点开看按模型的占比。
              数据一直在 /api/token-usage 里（by_model），之前只是没渲染。 */}
          <div onClick={models.length ? () => setShowModels(v => !v) : undefined}
            role={models.length ? 'button' : undefined} tabIndex={models.length ? 0 : undefined}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setShowModels(v => !v) } }}
            title={models.length ? '点开：今日各模型用量占比' : undefined}
            style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, cursor: models.length ? 'pointer' : 'default' }}>
            <span style={{ color: '#6b6b68' }}>今日 {fmtTokens(usage?.daily_tokens)} tokens{models.length ? (showModels ? ' ▾' : ' ▸') : ''}</span>
            {/* 有模型没配单价时 daily_cost 只是下限 —— 加 "+" 并说明，别让人以为这就是全部 */}
            <span style={{ color: unpriced.length ? '#d97706' : '#6b6b68' }}
              title={unpriced.length ? `以下模型未配置单价，未计入：${unpriced.join('、')}` : undefined}>
              {fmtCost(usage?.daily_cost)}{unpriced.length ? '+' : ''}
            </span>
          </div>
          {showModels && models.slice(0, 8).map((m: any) => (
            <div key={m.model} style={{ display: 'flex', gap: 6, fontSize: 10, color: '#9a9993' }}>
              <span className="truncate" style={{ flex: 1 }} title={m.model}>{m.model}</span>
              <span style={{ color: '#6b6b68' }}>{((m.share || 0) * 100).toFixed(0)}%</span>
              <span className="truncate" style={{ minWidth: 54, textAlign: 'right', whiteSpace: 'nowrap' }}
                title={m.cost === null || m.cost === undefined ? '未配置单价，无法计算费用' : undefined}
                onClick={() => { if (isUnpriced(m.cost)) navigate('/config') }}
                role={isUnpriced(m.cost) ? 'button' : undefined}>
                {isUnpriced(m.cost)
                  ? <span style={{ color: '#d97706' }}>未配置价格</span>
                  : fmtCost(m.cost)}
              </span>
            </div>
          ))}
          {/* 截断了就说出来 —— 否则看着像"就这几个模型在用" */}
          {showModels && models.length > 8 && (
            <div style={{ fontSize: 10, color: '#b5b2a8' }}>…另有 {models.length - 8} 个模型</div>
          )}
          {usage?.warning && <div style={{ fontSize: 10, color: '#dc2626' }}>{usage.warning}</div>}
        </div>
      </div>

      {sidebarCollapsed && (
        <button onClick={toggleSidebar} aria-label="展开侧边栏" style={{ position:'fixed',left:8,top:10,zIndex:10,background:'#f3f2ec',border:'none',borderRadius:6,color:'#9a9993',cursor:'pointer',padding:6 }}>
          <MessageSquare size={14}/>
        </button>
      )}

      <main className="main-area">
        <div className="main-scroll"><Outlet /></div>
      </main>
    </div>
  )
}
