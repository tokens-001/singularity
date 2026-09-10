/**
 * Usage.tsx — 用量页：按模型看谁在吃预算。
 *
 * 存在的理由：侧边栏那行总额回答不了"该换谁"。各模型单价差几十倍，
 * 只有按模型拆开才能看出预算被谁吃了。
 *
 * 数据全部来自现成的 GET /api/token-usage，**没有新增端点**；也没有 SSE 事件，
 * 只能轮询（30s，与侧边栏一致）。
 *
 * ⚠️ 费用一律走 lib/money.ts 的 fmtCost —— 没配单价的模型后端返回 null，
 * 前端必须显式显示"未配置价格"。渲染成 $0.00 就是在编造金额。
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

const COL = {
  model: { flex: 1, minWidth: 0 } as const,
  num: { minWidth: 62, textAlign: 'right' as const },
}

export default function Usage() {
  const [u, setU] = useState<any>({})
  const [loading, setLoading] = useState(true)
  const addToast = useToast()
  const navigate = useNavigate()

  const fetch = async (initial = false) => {
    if (initial) setLoading(true)
    try {
      const d = await api.tokenUsage()
      setU(d || {})
    } catch {
      addToast('加载用量失败', 'error')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetch(true)
    const t = setInterval(() => fetch(false), 30000)
    return () => clearInterval(t)
  }, [])

  const models: any[] = Array.isArray(u?.by_model) ? u.by_model : []
  const byML: any[] = Array.isArray(u?.by_model_level) ? u.by_model_level : []
  const unpriced: string[] = Array.isArray(u?.unpriced_models) ? u.unpriced_models : []

  const header = (
    <div className="flex-center gap-8" style={{ marginBottom: 12 }}>
      <h2 className="fs-13 fw-600" style={{ color: 'var(--text-primary)' }}>用量</h2>
      <span className="fs-11 text-muted">今日</span>
      <span className="flex-1" />
      <button onClick={() => fetch(true)} className="btn-icon" aria-label="刷新"><RefreshCw size={14} /></button>
    </div>
  )

  if (loading) {
    return (
      <div className="page-wrap">
        {header}
        {[0, 1, 2].map(i => <div key={i} className="skeleton skeleton-row" />)}
      </div>
    )
  }

  return (
    <div className="page-wrap">
      {header}

      {/* ── 合计 ── */}
      <div className="card-row" style={{ marginBottom: 16, borderBottom: 'none', padding: '10px 12px',
        background: 'var(--bg-secondary)', borderRadius: 'var(--radius)', border: '1px solid var(--border)' }}>
        <div className="flex-1">
          <div className="fs-10 text-muted">今日 tokens</div>
          <div className="mono fs-13">{fmtTokens(u?.daily_tokens)}</div>
        </div>
        <div className="flex-1">
          <div className="fs-10 text-muted">今日费用</div>
          <div className="mono fs-13" style={{ color: unpriced.length ? 'var(--accent-yellow)' : 'var(--text-primary)' }}>
            {fmtCost(u?.daily_cost)}{unpriced.length ? '+' : ''}
          </div>
        </div>
        <div className="flex-1">
          <div className="fs-10 text-muted">日预算</div>
          <div className="mono fs-13">${(u?.budget_daily || 0).toFixed(2)}</div>
        </div>
      </div>

      {unpriced.length > 0 && (
        <div className="fs-11" style={{ color: 'var(--accent-yellow)', marginBottom: 12 }}>
          ⚠ {unpriced.join('、')} 未配置单价，未计入上面的费用。
          <span role="button" tabIndex={0} style={{ textDecoration: 'underline', cursor: 'pointer', marginLeft: 6 }}
            onClick={() => navigate('/config')}
            onKeyDown={e => { if (e.key === 'Enter') navigate('/config') }}>去配置</span>
        </div>
      )}

      {/* ── 按模型 ── */}
      <div className="fs-11 fw-600" style={{ margin: '0 0 6px' }}>按模型</div>
      {models.length === 0 ? (
        <div className="fs-11 text-muted" style={{ padding: '12px 0' }}>今日暂无用量⋯</div>
      ) : (
        <>
          <div className="card-row" style={{ ...COL } as any}>
            <span className="fs-10 text-muted" style={COL.model}>模型</span>
            <span className="fs-10 text-muted" style={COL.num}>tokens</span>
            <span className="fs-10 text-muted" style={COL.num}>占比</span>
            <span className="fs-10 text-muted" style={COL.num}>任务</span>
            <span className="fs-10 text-muted" style={COL.num}>单价</span>
            <span className="fs-10 text-muted" style={{ minWidth: 74, textAlign: 'right' }}>费用</span>
          </div>
          {models.map((m: any) => (
            <div key={m.model} className="card-row" style={COL as any}>
              <span className="truncate mono" style={COL.model} title={m.model}>{m.model}</span>
              <span className="mono" style={COL.num}>{fmtTokens(m.tokens)}</span>
              <span style={COL.num}>
                <span className="mono">{((m.share || 0) * 100).toFixed(0)}%</span>
                {/* 占比条 —— 一眼看出谁在吃预算 */}
                <span style={{ display: 'block', height: 2, marginTop: 2, borderRadius: 1,
                  background: 'var(--border)' }}>
                  <span style={{ display: 'block', height: 2, borderRadius: 1,
                    background: 'var(--accent)', width: `${Math.round((m.share || 0) * 100)}%` }} />
                </span>
              </span>
              <span className="mono text-secondary" style={COL.num}>{m.tasks}</span>
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
      )}

      {/* ── 按 模型 × 层级 ── */}
      {byML.length > 0 && (
        <>
          <div className="fs-11 fw-600" style={{ margin: '18px 0 6px' }}>按 模型 × 层级</div>
          <div className="fs-10 text-muted" style={{ marginBottom: 4 }}>
            回答"这个模型只在架构阶段贵，还是全程都贵"（只列 token，不重复算钱）
          </div>
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
        单价单位：USD / 百万 token（混合价 —— 系统只记总 token，不区分输入/输出）。
        费用按单价实时计算，补上单价后**历史用量会一起变对**，无需迁移。
      </div>
    </div>
  )
}
