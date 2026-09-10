/**
 * Usage.tsx — 用量统计。
 *
 * 结构对照 ZCode 的「使用统计」页：
 *   时间段切换 → 指标卡 → 活跃度 → 热力图 → 每日趋势 → 按模型 → 今日详情
 *
 * **两条数据源，别混**：
 *   · 今日详情  ← GET /api/token-usage（每 30s 轮询，含未配置价格的实时提示）
 *   · 上面各段  ← GET /api/usage-history?range=（切范围时取 + 手动刷新）
 *   范围区**绝不复用今日的数字**，反之亦然。
 *
 * ⚠️ 金额一律走 lib/money.ts 的 fmtCost / fmtPrice —— 没配单价的模型后端返回 null，
 * 前端必须显式显示"未配置价格"。渲染成 $0.00 就是在编造金额。
 *
 * **抄不了的（ZCode 有、我们没有，所以不显示而不是编）**：输入/缓存/输出四段拆分、
 * 缓存命中率、工具用量、会话数/消息数（用"任务数"代替并如实标注）、厂商额度。
 * 图也不用 SVG / 图表库 —— 柱和方格正是 flex div 擅长的，全仓只有 7 个运行时依赖。
 */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshCw } from 'lucide-react'
import { api } from '../lib/api'
import { useToast } from '../lib/toast'
import { fmtCost, isUnpriced } from '../lib/money'

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return String(n || 0)
}

/** 秒 → 人话。0 返回空串，调用方据此不渲染 —— 绝不显示成 0.0h。 */
function fmtDuration(s: number): string {
  if (!s || s <= 0) return ''
  if (s < 60) return `${Math.round(s)}秒`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}分钟`
  return `${Math.floor(m / 60)}小时${m % 60}分`
}

const RANGES: { v: string; label: string }[] = [
  { v: 'all', label: '全部' },
  { v: '30d', label: '近30天' },
  { v: '7d', label: '近7天' },
]

const COL = {
  model: { flex: 1, minWidth: 0 } as const,
  num: { minWidth: 62, textAlign: 'right' as const },
}

// 5 档强度。0 档用底色，1-4 用 --accent 递增 alpha（与页面占比条同色，白拿视觉一致）
const LEVEL_BG = ['var(--bg-tertiary)', 'rgba(37,99,235,0.15)',
                  'rgba(37,99,235,0.35)', 'rgba(37,99,235,0.6)', '#2563eb']

function MetricCard({ label, value, sub, warn }: {
  label: string; value: string; sub?: string; warn?: boolean
}) {
  return (
    <div className="flex-1" style={{ minWidth: 92 }}>
      <div className="fs-10 text-muted">{label}</div>
      <div className="mono fs-13 truncate" title={value}
        style={{ color: warn ? 'var(--accent-yellow)' : 'var(--text-primary)' }}>{value}</div>
      {sub && <div className="fs-10 text-muted truncate" title={sub}>{sub}</div>}
    </div>
  )
}

/** GitHub 那种周列热力图。**data-day / data-tokens 是刻意留的测试钩子** ——
 *  jsdom 不做布局，靠计算样式断言测不出东西。 */
function Heatmap({ days, peak }: { days: any[]; peak: number }) {
  if (!days.length) return null
  const pad = new Date(days[0].date + 'T00:00:00').getDay()   // 0=周日
  const cells: (any | null)[] = [...Array(pad).fill(null), ...days]
  const weeks: (any | null)[][] = []
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7))

  const level = (t: number) =>
    t <= 0 ? 0 : Math.min(4, Math.max(1, Math.ceil((t / (peak || 1)) * 4)))

  return (
    <div>
      <div style={{ display: 'flex', gap: 3, overflowX: 'auto', paddingBottom: 4 }}>
        {weeks.map((w, wi) => (
          <div key={wi} style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            {w.map((d, di) => d === null
              ? <div key={di} style={{ width: 11, height: 11 }} />
              : <div key={di} data-day={d.date} data-tokens={d.tokens}
                  title={`${d.date}\n${fmtTokens(d.tokens)} tokens · ${d.tasks} 次任务`}
                  style={{ width: 11, height: 11, borderRadius: 2,
                           background: LEVEL_BG[level(d.tokens)] }} />)}
          </div>
        ))}
      </div>
      <div className="flex-center gap-4 fs-10 text-muted" style={{ marginTop: 6 }}>
        <span>少</span>
        {LEVEL_BG.map((c, i) => (
          <span key={i} style={{ width: 10, height: 10, borderRadius: 2,
                                 background: c, display: 'inline-block' }} />
        ))}
        <span>多</span>
        <span style={{ marginLeft: 10 }}>最高强度</span>
        <span style={{ width: 10, height: 10, borderRadius: 2,
                       background: '#2563eb', display: 'inline-block' }} />
      </div>
    </div>
  )
}

/** 每日柱状。**peak === 0 时渲染空状态，绝不吐一排 NaN% 高度。** */
function TrendBars({ days, peakDate }: { days: any[]; peakDate: string | null }) {
  const peak = Math.max(0, ...days.map(d => d.tokens))
  if (!days.length || peak === 0) {
    return <div className="fs-11 text-muted" style={{ padding: '10px 0' }}>这段时间还没有用量⋯</div>
  }
  const step = Math.ceil(days.length / 6)
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 2, height: 90 }}>
        {days.map(d => {
          const isPeak = d.date === peakDate
          return (
            <div key={d.date} data-day={d.date} data-peak={isPeak ? '1' : undefined}
              title={`${d.date}\n${fmtTokens(d.tokens)} tokens · ${d.tasks} 次任务`}
              style={{ flex: 1, minHeight: 1, borderRadius: '2px 2px 0 0',
                       height: `${Math.max(1, Math.round((d.tokens / peak) * 100))}%`,
                       background: d.tokens === 0 ? 'var(--border)'
                                 : isPeak ? '#2563eb' : 'rgba(37,99,235,0.35)' }} />
          )
        })}
      </div>
      <div style={{ display: 'flex', gap: 2, marginTop: 4 }}>
        {days.map((d, i) => (
          <span key={d.date} className="fs-10 text-muted"
            style={{ flex: 1, textAlign: 'center', whiteSpace: 'nowrap', overflow: 'hidden' }}>
            {i % step === 0 ? d.date.slice(5).replace('-', '/') : ''}
          </span>
        ))}
      </div>
    </div>
  )
}

/** 模型表。历史区与今日区共用，靠 showTasks 区分 —— 历史桶里没存按模型的任务数，
 *  那一列就不给（不编）。 */
function ModelRows({ rows, showTasks }: { rows: any[]; showTasks: boolean }) {
  const navigate = useNavigate()
  const cols = showTasks
    ? [COL.model, COL.num, COL.num, COL.num, COL.num, { minWidth: 74, textAlign: 'right' as const }]
    : [COL.model, COL.num, COL.num, COL.num, { minWidth: 74, textAlign: 'right' as const }]
  return (
    <>
      <div className="card-row" style={COL as any}>
        <span className="fs-10 text-muted" style={cols[0] as any}>模型</span>
        <span className="fs-10 text-muted" style={cols[1] as any}>tokens</span>
        <span className="fs-10 text-muted" style={cols[2] as any}>占比</span>
        {showTasks && <span className="fs-10 text-muted" style={cols[3] as any}>任务</span>}
        <span className="fs-10 text-muted" style={cols[showTasks ? 4 : 3] as any}>单价</span>
        <span className="fs-10 text-muted" style={cols[showTasks ? 5 : 4] as any}>费用</span>
      </div>
      {rows.map((m: any) => (
        <div key={m.model} className="card-row" style={COL as any}>
          <span className="truncate mono" style={COL.model} title={m.model}>{m.model}</span>
          <span className="mono" style={COL.num}>{fmtTokens(m.tokens)}</span>
          <span style={COL.num}>
            <span className="mono">
              {showTasks ? ((m.share || 0) * 100).toFixed(0) : Math.round((m.share || 0) * 100)}%
            </span>
            <span style={{ display: 'block', height: 2, marginTop: 2, borderRadius: 1,
              background: 'var(--border)' }}>
              <span style={{ display: 'block', height: 2, borderRadius: 1,
                background: 'var(--accent)', width: `${Math.round((m.share || 0) * 100)}%` }} />
            </span>
          </span>
          {showTasks && <span className="mono text-secondary" style={COL.num}>{m.tasks}</span>}
          <span className="mono text-secondary" style={COL.num}>
            {isUnpriced(m.price) ? <span className="text-muted">—</span> : `$${m.price.toFixed(2)}/M`}
          </span>
          <span className="mono" style={{ minWidth: 74, textAlign: 'right',
            color: isUnpriced(m.cost) ? 'var(--accent-yellow)' : 'var(--text-primary)' }}>
            {isUnpriced(m.cost)
              ? <span role="button" tabIndex={0} style={{ cursor: 'pointer' }}
                  onClick={() => navigate('/config')}>未配置价格</span>
              : fmtCost(m.cost)}
          </span>
        </div>
      ))}
    </>
  )
}

export default function Usage() {
  const [u, setU] = useState<any>({})        // 今日（token-usage）
  const [h, setH] = useState<any>(null)      // 历史（usage-history）
  const [range, setRange] = useState('30d')
  const [loading, setLoading] = useState(true)
  const addToast = useToast()
  const navigate = useNavigate()

  const fetchToday = async (initial = false) => {
    if (initial) setLoading(true)
    try { setU((await api.tokenUsage()) || {}) }
    catch { addToast('加载用量失败', 'error') }
    finally { setLoading(false) }
  }
  const fetchHistory = async (r: string = range) => {
    try { setH((await api.usageHistory(r)) || null) }
    catch { addToast('加载历史用量失败', 'error') }
  }

  useEffect(() => {
    fetchToday(true)
    const t = setInterval(() => fetchToday(false), 30000)   // 只轮询今日
    return () => clearInterval(t)
  }, [])
  // 历史不轮询：数字在光标底下自己变是噪声。切范围 + 手动刷新足够。
  useEffect(() => { fetchHistory(range) }, [range])

  const models: any[] = Array.isArray(u?.by_model) ? u.by_model : []
  const byML: any[] = Array.isArray(u?.by_model_level) ? u.by_model_level : []
  const unpriced: string[] = Array.isArray(u?.unpriced_models) ? u.unpriced_models : []

  const hDays: any[] = h?.days || []
  const hModels: any[] = h?.models || []
  const hTotals = h?.totals || {}
  const hAct = h?.activity || {}
  const hUnpriced: string[] = hTotals.unpriced_models || []
  const rangeLabel = RANGES.find(r => r.v === range)?.label || range

  if (loading) {
    return (
      <div className="page-wrap">
        <h2 className="fs-13 fw-600" style={{ marginBottom: 12 }}>用量</h2>
        {[0, 1, 2, 3].map(i => <div key={i} className="skeleton skeleton-row" />)}
      </div>
    )
  }

  return (
    <div className="page-wrap">
      {/* ── 头部 + 时间段 ── */}
      <div className="flex-center gap-8" style={{ marginBottom: 4 }}>
        <h2 className="fs-13 fw-600" style={{ color: 'var(--text-primary)' }}>用量</h2>
        <div className="flex-center gap-4">
          {RANGES.map(r => (
            <button key={r.v} onClick={() => setRange(r.v)} className="btn-sm"
              style={range === r.v
                ? { background: 'var(--accent)', color: '#fff', borderColor: 'var(--accent)' }
                : undefined}>{r.label}</button>
          ))}
        </div>
        <span className="flex-1" />
        <button onClick={() => { fetchToday(true); fetchHistory() }} className="btn-icon"
          aria-label="刷新"><RefreshCw size={14} /></button>
      </div>
      {h?.earliest && (
        <div className="fs-10 text-muted" style={{ marginBottom: 12 }}>
          统计自 {h.earliest}（只保留最近 400 天）
        </div>
      )}

      {/* ── 历史区：数据源是 /api/usage-history，与下面的今日区互不混用 ── */}
      {h && (
        <>
          <div className="fs-11 fw-600" style={{ margin: '0 0 6px' }}>{rangeLabel}</div>
          <div className="card-row flex-wrap" style={{ gap: 12, padding: '10px 12px',
            background: 'var(--bg-secondary)', borderRadius: 'var(--radius)',
            border: '1px solid var(--border)', marginBottom: 16 }}>
            <MetricCard label="Token 用量" value={fmtTokens(hTotals.tokens || 0)} />
            {/* ZCode 这里是"会话数" —— 我们没有会话概念，如实叫"任务数" */}
            <MetricCard label="任务数" value={String(hTotals.tasks || 0)} />
            <MetricCard label="活跃天数" value={`${hTotals.active_days || 0} 天`} />
            <MetricCard label="主力模型"
              value={hModels[0] ? hModels[0].model : '—'}
              sub={hModels[0] ? `${Math.round((hModels[0].share || 0) * 100)}% 占比` : undefined} />
            {/* 时长只在有数据时出现 —— 融合/取消路径不报 elapsed，0 是"没采到"不是"没花时间" */}
            {hAct.elapsed_s > 0 && <MetricCard label="总使用时长" value={fmtDuration(hAct.elapsed_s)} />}
            {hAct.max_elapsed_s > 0 && <MetricCard label="最长任务" value={fmtDuration(hAct.max_elapsed_s)} />}
          </div>

          <div className="fs-11 fw-600" style={{ margin: '0 0 6px' }}>活跃度</div>
          <div className="card-row flex-wrap" style={{ gap: 12 }}>
            <MetricCard label="累计 tokens" value={fmtTokens(hTotals.tokens || 0)} />
            <MetricCard label="最高单日"
              value={hAct.peak_day ? fmtTokens(hAct.peak_day.tokens) : '—'}
              sub={hAct.peak_day ? hAct.peak_day.date.slice(5).replace('-', '/') : undefined} />
            <MetricCard label="当前连续" value={`${hAct.current_streak || 0} 天`} />
            <MetricCard label="最长连续" value={`${hAct.longest_streak || 0} 天`} />
            <MetricCard label="高峰时段"
              value={hAct.peak_hour === null || hAct.peak_hour === undefined
                ? '—'
                : `${String(hAct.peak_hour).padStart(2, '0')}:00–${String(hAct.peak_hour + 1).padStart(2, '0')}:00`} />
          </div>

          <div className="fs-11 fw-600" style={{ margin: '18px 0 6px' }}>Token 活动</div>
          <Heatmap days={hDays} peak={hAct.peak_day?.tokens || 0} />
          {hAct.peak_day && (
            <div className="fs-10 text-muted" style={{ marginTop: 4 }}>
              最忙的一天是 {hAct.peak_day.date}，约 {fmtTokens(hAct.peak_day.tokens)} tokens。
            </div>
          )}

          <div className="fs-11 fw-600" style={{ margin: '18px 0 6px' }}>每日 Token 趋势</div>
          <TrendBars days={hDays} peakDate={hAct.peak_day?.date || null} />

          <div className="fs-11 fw-600" style={{ margin: '18px 0 6px' }}>按模型（{rangeLabel}）</div>
          {hModels.length === 0 ? (
            <div className="fs-11 text-muted" style={{ padding: '12px 0' }}>这段时间暂无用量⋯</div>
          ) : (
            <>
              <ModelRows rows={hModels} showTasks={false} />
              {hUnpriced.length > 0 && (
                <div className="fs-10" style={{ color: 'var(--accent-yellow)', marginTop: 6 }}>
                  ⚠ {hUnpriced.join('、')} 未配置单价，未计入上面的费用。
                </div>
              )}
            </>
          )}
        </>
      )}

      {/* ── 今日详情（数据源 /api/token-usage，与上面的范围区无关） ── */}
      <div className="fs-11 fw-600" style={{ margin: '24px 0 6px' }}>今日详情</div>
      <div className="card-row" style={{ marginBottom: 12, padding: '10px 12px',
        background: 'var(--bg-secondary)', borderRadius: 'var(--radius)',
        border: '1px solid var(--border)' }}>
        <div className="flex-1">
          <div className="fs-10 text-muted">今日 tokens</div>
          <div className="mono fs-13">{fmtTokens(u?.daily_tokens)}</div>
        </div>
        <div className="flex-1">
          <div className="fs-10 text-muted">今日费用</div>
          <div className="mono fs-13"
            style={{ color: unpriced.length ? 'var(--accent-yellow)' : 'var(--text-primary)' }}>
            {fmtCost(u?.daily_cost)}{unpriced.length ? '+' : ''}
          </div>
        </div>
        <div className="flex-1">
          <div className="fs-10 text-muted">日预算</div>
          <div className="mono fs-13">${(u?.budget_daily || 0).toFixed(2)}</div>
        </div>
      </div>

      <div className="fs-11 fw-600" style={{ margin: '0 0 6px' }}>今日 按模型</div>
      {models.length === 0 ? (
        <div className="fs-11 text-muted" style={{ padding: '12px 0' }}>今日暂无用量⋯</div>
      ) : (
        <>
          <ModelRows rows={models} showTasks />
          {unpriced.length > 0 && (
            <div className="fs-10" style={{ color: 'var(--accent-yellow)', marginTop: 6 }}>
              ⚠ {unpriced.join('、')} 未配置单价，未计入上面的费用。
            </div>
          )}
        </>
      )}

      {byML.length > 0 && (
        <>
          <div className="fs-11 fw-600" style={{ margin: '18px 0 6px' }}>今日 按 模型 × 层级</div>
          {byML.map((r: any) => (
            <div key={`${r.model}-${r.level}`} className="card-row">
              <span className="truncate mono flex-1" title={r.model}>{r.model}</span>
              <span className="card-tag">{r.level}</span>
              <span className="mono" style={{ minWidth: 62, textAlign: 'right' }}>{fmtTokens(r.tokens)}</span>
              <span className="mono text-secondary" style={{ minWidth: 46, textAlign: 'right' }}>{r.tasks} 次</span>
            </div>
          ))}
        </>
      )}

      <div className="fs-10 text-muted" style={{ marginTop: 16, lineHeight: 1.7 }}>
        单价单位：USD / 百万 token（混合价 —— 系统只记总 token，不区分输入/输出，
        所以 ZCode 那种 输入/缓存/输出 分段这里给不了）。费用按单价实时计算，
        补上单价后**历史用量会一起变对**，无需迁移。<br />
        历史只保留最近 400 天，且**从本功能上线那天才开始攒** —— 之前的日子没有落过盘。
      </div>
    </div>
  )
}
