/**
 * Alerts.tsx — 告警：把「常驻」从「事件」里分出来。
 *
 * **为什么要有这一页**：真机实测一段 26 分钟的窗口里 25 条告警，**22 条（88%）
 * 挤在两个 key 上**（`collect_changes` / `constraints_checklist_fallback`）。
 * 常亮不等于"最近问题多"，是**判据跟配置脱节** —— 它该出现在这一栏，
 * 不该淹在事故流里把真事故盖住。
 *
 * 数据源只有一个：`GET /api/status` 的 `alert_summary`
 * （后端 `witness.alert_summary()`，已按 key 归并、按 n 降序）。
 * **前端不做任何归并/排序** —— 归并规则在后端一处，抄一份必漂。
 *
 * 不轮询：这是"现在有什么常驻毛病"的快照，不是盯着看的看板。数字在光标底下
 * 自己变是噪声（同用量页）。所以留**手动刷新** + 一个"数据截至"的时间戳，
 * 让"旧"这件事看得见。
 */
import { useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { api } from '../lib/api'
import { useToast } from '../lib/toast'

interface AlertGroup {
  key: string
  scopes: string[]
  n: number
  first_ts: number
  last_ts: number
  chronic: boolean
  sample: string
}

/** 相对时间。只用于"多久没动过"，精确到秒没意义。 */
function ago(ts: number, now: number): string {
  const s = Math.max(0, now - ts)
  if (s < 60) return `${Math.floor(s)} 秒前`
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`
  return `${Math.floor(s / 86400)} 天前`
}

function clock(ts: number): string {
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function Row({ g, now }: { g: AlertGroup; now: number }) {
  return (
    <div style={{ display: 'flex', gap: 10, padding: '7px 10px', borderBottom: '1px solid var(--border)' }}>
      <div style={{ width: 56, flexShrink: 0, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}
           title="这个 key 下的告警条数">
        <span className="text-muted">×</span>{g.n}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontWeight: 600, wordBreak: 'break-all' }}>{g.key || <span className="text-muted">（无 key）</span>}</div>
        <div className="text-muted" style={{ fontSize: 12, wordBreak: 'break-all' }}>{g.sample}</div>
      </div>
      <div style={{ width: 210, flexShrink: 0, fontSize: 12, textAlign: 'right' }} className="text-muted">
        {/* 范围不丢：同一个常驻条件可能被两个调用方各报一遍，后端按 key 归并了，
            这里把"从哪儿报的"如实显示出来。 */}
        {g.scopes?.length ? <div>{g.scopes.join(' / ')}</div> : null}
        <div title={`${clock(g.first_ts)} → ${clock(g.last_ts)}`}>
          首发 {clock(g.first_ts)} · 最近 {ago(g.last_ts, now)}
        </div>
      </div>
    </div>
  )
}

function Section({ title, hint, groups, now, empty }: {
  title: string; hint: string; groups: AlertGroup[]; now: number; empty: string
}) {
  return (
    <div style={{ marginBottom: 22 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 6 }}>
        <h3 style={{ margin: 0, fontSize: 15 }}>{title}</h3>
        <span className="text-muted" style={{ fontSize: 12 }}>{hint}</span>
      </div>
      <div style={{ border: '1px solid var(--border)', borderRadius: 6 }}>
        {groups.length
          ? groups.map(g => <Row key={g.key || g.sample} g={g} now={now} />)
          : <div className="text-muted" style={{ padding: 12, fontSize: 13 }}>{empty}</div>}
      </div>
    </div>
  )
}

export default function Alerts() {
  const [groups, setGroups] = useState<AlertGroup[] | null>(null)
  const [at, setAt] = useState(0)
  const addToast = useToast()

  // ⚠️ `addToast` **每次渲染都是新函数**（`useToast` 返回箭头函数，不是 useCallback 包过的），
  // 所以它绝不能进任何 effect 的依赖 —— 进去就是：渲染 → 依赖变 → effect 重跑 → setState
  // → 再渲染，**无限循环拉接口**。（2026-09-13 被 Alerts.test.tsx 逮到。）
  // 同理 `load` 也不能当 `useEffect` 的依赖。只挂载时拉一次，之后手动刷。
  const load = async () => {
    try {
      const d: any = await api.status()
      setGroups(d?.alert_summary || [])
      setAt(Date.now() / 1000)
    } catch (e: any) {
      addToast(`加载告警失败：${e?.message || e}`, 'error')
    }
  }

  useEffect(() => { load() }, [])   // eslint-disable-line react-hooks/exhaustive-deps

  if (groups === null) return <div className="text-muted" style={{ padding: 24 }}>加载中…</div>

  // 分组是后端给的（chronic = n ≥ chronic_min）。这里只做**呈现上的**拆分。
  const chronic = groups.filter(g => g.chronic)
  const events = groups.filter(g => !g.chronic)
  const now = Date.now() / 1000

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <h2 style={{ margin: 0, fontSize: 17 }}>告警</h2>
        <span className="text-muted" style={{ fontSize: 12 }}>
          共 {groups.length} 组 · 数据截至 {clock(at)}
        </span>
        <button onClick={load} title="重新拉取"
                style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 5,
                         background: 'none', border: '1px solid var(--border)', borderRadius: 4,
                         padding: '3px 9px', cursor: 'pointer', color: 'inherit' }}>
          <RefreshCw size={13} /> 刷新
        </button>
      </div>

      <Section title="常驻" hint="同一个 key 反复出现 —— 多半是判据跟配置脱节，不是「刚出了新事故」"
               groups={chronic} now={now}
               empty="没有常驻告警。" />
      <Section title="事件" hint="出现次数还没到常驻线，按发生次数排"
               groups={events} now={now}
               empty="没有事件级告警。" />
    </div>
  )
}
