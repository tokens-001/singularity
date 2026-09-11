/**
 * Usage.tsx — 用量统计：就一张「按模型」的表 + 时间段切换。
 *
 * 砍掉的东西（曾经有过，实测是噪声）：指标卡、活跃度（连续天数/高峰时段）、
 * 热力图、每日趋势柱、以及和范围区重复的"今日详情"块。
 * 用户要的是"各模型的用量"，不是一整套分析看板。
 *
 * 数据源只有一个：GET /api/usage-history?range=（切范围时取 + 手动刷新）。
 * 不再轮询 —— 统计页的数字在光标底下自己变是噪声。
 *
 * ⚠️ 金额一律走 lib/money.ts 的 fmtCost —— 没配单价的模型后端返回 null，
 * 必须显式显示"未配置价格"。**任何情况下都不许把"算不出来"渲染成 $0.00。**
 */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshCw } from 'lucide-react'
import { api } from '../lib/api'
import { useToast } from '../lib/toast'
import { fmtCost, fmtPriceValue, isUnpriced, PRICE_UNIT } from '../lib/money'
import { modelDisplay } from './Config'

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return String(n || 0)
}

/** 日历口径（本周 = 周一起，本月 = 当月 1 号起），不是"最近 N 天"的滚动窗口。
 *  由宽到窄排列。 */
const RANGES: { v: string; label: string }[] = [
  { v: 'all', label: '累计至今' },
  { v: 'month', label: '本月' },
  { v: 'week', label: '本周' },
  { v: 'today', label: '今日' },
]

// 列宽必须是**固定值**，不能只给 minWidth —— 这套表格是 flex 行，
// 只给 minWidth 时每列宽度由**内容**决定：`$0.48/百万token` 把后面挤走、
// `—` 又缩回来，于是每行的列对不齐（实测"各显示各的"）。
// flexShrink: 0 保证宁可整体横向滚动，也不让内容改列宽。
const COL = {
  model: { flex: 1, minWidth: 0 } as const,
  num: { width: 72, flexShrink: 0, textAlign: 'right' as const },
}

const COST_COL = { width: 84, flexShrink: 0, textAlign: 'right' as const }

/** 供应商状态 → 中文。只显示非 active 的 —— 好端端的不用打扰。 */
const STATUS_CN: Record<string, string> = {
  quota_exhausted: '配额耗尽',
  disabled: '已禁用',
}

export default function Usage() {
  const [h, setH] = useState<any>(null)
  const [range, setRange] = useState('all')
  const [loading, setLoading] = useState(true)
  const addToast = useToast()
  const navigate = useNavigate()

  const load = async (r: string, initial = false) => {
    if (initial) setLoading(true)
    try { setH((await api.usageHistory(r)) || null) }
    catch { addToast('加载用量失败', 'error') }
    finally { setLoading(false) }
  }

  useEffect(() => { load(range, true) }, [range])

  const models: any[] = h?.models || []
  const totals = h?.totals || {}
  const unpriced: string[] = totals.unpriced_models || []
  // 按天：后端一直在算 `days`（稠密补零、只保留最近 400 天），**页面以前一个字不显示** ——
  // 于是你只能看到"当前汇总"，看不出哪天在烧、烧了多少。只画最近 30 天，400 根柱子是糊的。
  const days: any[] = h?.days || []
  const trend = days.slice(-30)
  const peak = Math.max(1, ...trend.map((d: any) => d.tokens || 0))

  // 行渲染抽出来：下面要按「已使用 / 未使用」分两组，复制一遍这坨 JSX 迟早走样。
  const renderRow = (m: any) => (
    <div key={m.model} className="card-row" style={COL as any}>
      <span className="flex-center gap-4" style={COL.model}>
        {/* 模型名走统一出口 modelDisplay —— 这里原来是裸的 id（`deepseek-v4-flash`），
            而模型页显示 `DeepSeek V4 Flash`，同一个模型两页两个名字。
            原始 id 留在 title 里，要精确值的时候鼠标一悬就有。 */}
        <span className="truncate" title={m.model}>{modelDisplay(m.model) || m.model}</span>
        {/* 非 active 的供应商状态要露出来 —— 否则你只会看到"未使用"，
            而不知道是没跑过、还是账号欠费了。 */}
        {STATUS_CN[m.provider_status] && (
          <span className="card-tag" style={{ color: 'var(--accent-yellow)', flexShrink: 0 }}
            title={`供应商 ${m.provider} 状态：${m.provider_status}`}>
            {STATUS_CN[m.provider_status]}
          </span>
        )}
      </span>
      <span className="mono" style={COL.num}>
        {m.used ? fmtTokens(m.tokens) : <span className="text-muted">—</span>}
      </span>
      <span style={COL.num}>
        {m.used ? (
          <>
            <span className="mono">{Math.round((m.share || 0) * 100)}%</span>
            <span style={{ display: 'block', height: 2, marginTop: 2, borderRadius: 1,
              background: 'var(--border)' }}>
              <span style={{ display: 'block', height: 2, borderRadius: 1,
                background: 'var(--accent)', width: `${Math.round((m.share || 0) * 100)}%` }} />
            </span>
          </>
        ) : <span className="text-muted">—</span>}
      </span>
      <span className="mono text-secondary" style={COL.num}>
        {/* 走 money.ts，别在这儿自己拼 —— 这里原来手写 `$${...}/M`，
            绕过了那个「金额唯一出口」，于是单位改全了它也不跟着变。
            数值不带单位：单位在表头。 */}
        {isUnpriced(m.price) ? <span className="text-muted">—</span> : fmtPriceValue(m.price)}
      </span>
      <span className="mono" style={{ ...COST_COL,
        color: isUnpriced(m.cost) && m.used ? 'var(--accent-yellow)' : 'var(--text-primary)' }}>
        {/* 没用过的模型没有费用可报 —— 显示"—"，别拿 0 或"未配置价格"充数 */}
        {!m.used ? <span className="text-muted">—</span>
          : isUnpriced(m.cost)
            ? <span role="button" tabIndex={0} style={{ cursor: 'pointer' }}
                onClick={() => navigate('/config')}>未配置价格</span>
            : fmtCost(m.cost)}
      </span>
    </div>
  )
  // 一个模型都没配单价时，合计**没有数可报** —— 这时必须显示"未配置价格"，
  // 而不是 totals.cost 那个 0（那会渲染成看着可信的 $0.0000）。
  const anyPriced = models.some((m: any) => !isUnpriced(m.cost))

  return (
    <div className="page-wrap">
      {/* 左右 10px 是跟 .card-row 对齐的 —— .page-wrap 自己没有横向内边距，
          不补的话标题会顶着表格左边 10px 开外，整页左边缘是毛的 */}
      <div className="flex-center gap-8" style={{ marginBottom: 4, padding: '0 10px' }}>
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
        <button onClick={() => load(range, true)} className="btn-icon" aria-label="刷新">
          <RefreshCw size={14} />
        </button>
      </div>

      {/* 历史只留最近 400 天 —— 截断过的"全部"不加说明就是在撒谎 */}
      {h?.earliest && (
        <div className="fs-10 text-muted" style={{ marginBottom: 12, padding: '0 10px' }}>
          统计自 {h.earliest}（只保留最近 400 天）
        </div>
      )}

      {loading ? (
        [0, 1, 2].map(i => <div key={i} className="skeleton skeleton-row" />)
      ) : models.length === 0 ? (
        <div className="fs-11 text-muted" style={{ padding: '12px 10px' }}>这段时间暂无用量⋯</div>
      ) : (
        <>
          {trend.length > 1 && (
            <div style={{ marginBottom: 16, padding: '0 10px' }}>
              <div className="fs-10 text-muted" style={{ marginBottom: 5 }}>
                按天（最近 {trend.length} 天{trend.length < days.length ? `，共 ${days.length} 天` : ''}
                {' · '}活跃 {totals.active_days ?? 0} 天{' · '}共 {totals.tasks ?? 0} 个任务）
              </div>
              <div style={{ display: 'flex', alignItems: 'flex-end', gap: 1, height: 44 }}>
                {trend.map((d: any) => (
                  <div key={d.date}
                    title={`${d.date}：${fmtTokens(d.tokens)} · ${d.tasks} 个任务`}
                    style={{
                      flex: 1, minWidth: 1, borderRadius: 1,
                      // 空白天留 2px 灰底 —— 否则"那天没跑"和"那天不存在"分不出来
                      height: `${Math.max(d.tokens ? 4 : 2, (d.tokens / peak) * 100)}%`,
                      background: d.tokens ? 'var(--accent)' : 'var(--border)',
                    }} />
                ))}
              </div>
            </div>
          )}

          <div className="card-row" style={COL as any}>
            <span className="fs-10 text-muted" style={COL.model}>模型</span>
            <span className="fs-10 text-muted" style={COL.num}>tokens</span>
            <span className="fs-10 text-muted" style={COL.num}>占比</span>
            {/* 单位写在表头一次，别逐格重复 —— 那既撑宽列也是噪声 */}
            <span className="fs-10 text-muted" style={COL.num}>单价{PRICE_UNIT}</span>
            <span className="fs-10 text-muted" style={COST_COL}>费用</span>
          </div>

          {[
            { title: '使用中', rows: models.filter((m: any) => m.used) },
            { title: '未使用', rows: models.filter((m: any) => !m.used) },
          ].map(g => g.rows.length === 0 ? null : (
            <div key={g.title}>
              <div className="fs-10 text-muted" style={{ padding: '10px 10px 4px' }}>
                {g.title}（{g.rows.length}）
              </div>
              {g.rows.map(renderRow)}
            </div>
          ))}

          <div className="card-row" style={{ ...COL, fontWeight: 600 } as any}>
            <span className="flex-1">合计</span>
            <span className="mono" style={COL.num}>{fmtTokens(totals.tokens || 0)}</span>
            <span style={COL.num} />
            <span style={COL.num} />
            <span className="mono" style={{ ...COST_COL,
              color: unpriced.length ? 'var(--accent-yellow)' : 'var(--text-primary)' }}>
              {fmtCost(anyPriced ? totals.cost : null)}{unpriced.length ? '+' : ''}
            </span>
          </div>

          {unpriced.length > 0 && (
            <div className="fs-10" style={{ color: 'var(--accent-yellow)', marginTop: 6, padding: '0 10px' }}>
              ⚠ {unpriced.join('、')} 未配置单价，未计入上面的费用。
              <span role="button" tabIndex={0}
                style={{ marginLeft: 6, textDecoration: 'underline', cursor: 'pointer' }}
                onClick={() => navigate('/config')}>去配置</span>
            </div>
          )}
        </>
      )}

      <div className="fs-10 text-muted" style={{ marginTop: 16, lineHeight: 1.7, padding: '0 10px' }}>
        单价单位：USD / 百万 token（混合价 —— 系统只记总 token，不区分输入/输出）。
        费用按单价实时计算，补上单价后历史用量会一起变对。<br />
        已覆盖：任务执行、观察者对话、任务分类、架构融合、记忆整合、目标循环。
      </div>
    </div>
  )
}
