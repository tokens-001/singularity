import { memo } from 'react'
import { CheckCircle2, XCircle, Loader2, RotateCcw, FolderOpen, FileText } from 'lucide-react'

export interface ToolLog { tool: string; kind: string; msg: string; ts: number }
export interface ProgressItem {
  id: string; desc: string; status: string; ts: number; route_type?: string
  duration?: number; error?: string; files?: string[]; verdict?: string; logs?: ToolLog[]
}

interface Props { t: ProgressItem; onRetry: (id: string) => void; onReveal: (file: string) => void }

/**
 * 验收结论的三态渲染。**判据是"是不是成功"，不是"有没有值"。**
 *
 * ⚠️ 原来的判据是 `t.verdict && t.verdict !== '?'`，样式**写死绿色 + ✓**。
 * 而后端 `verdict` 的取值是 `validator.py` 里那几个：
 * **`通过` / `阻断` / `gate失败` / `信息不足` / `人工复核` / `未知`**
 * ⇒ 任务**被阻断**、门禁**失败**、需要**人工复核**时，卡片上渲染出来的是
 * 绿色的「✓ 阻断」「✓ gate失败」——**和 `GatePanel` 那条 GATE3 绿灯是同一个病**
 * （判据落在"非空"而不是"成功"上，默认那一侧是好消息）。
 *
 * 配色跟 `GatePanel`/`conformance` 同口径：灰=没数据 · 琥珀=核不了 · 绿=通过 · 红=不通过。
 */
function verdictChip(v: string): { bg: string; fg: string; mark: string } {
  if (v === '通过') return { bg: '#eaf6ec', fg: '#16a34a', mark: '✓' }
  if (v === '阻断' || v === 'gate失败') return { bg: '#fdeaea', fg: '#dc2626', mark: '✕' }
  // `信息不足` / `人工复核` / `未知` / 以及**任何将来新增、这里不认识的档**
  // ⇒ 一律琥珀"待人工"，**不许落到绿**。不认识 ≠ 通过。
  return { bg: '#fef3c7', fg: '#b45309', mark: '?' }
}

/** 任务进度卡。memo：只有这张卡自己的数据变了才重渲染（日志追加时其他卡不动）。 */
export const TaskCard = memo(function TaskCard({ t, onRetry, onReveal }: Props) {
  const done = t.status === 'done'
  const fail = t.status === 'failed' || t.status === 'cancelled'
  return (
    <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start', padding: '10px 12px',
      background: '#fff', border: '1px solid var(--border)', borderRadius: 10, boxShadow: 'var(--shadow-sm)', marginBottom: 6 }}>
      <span style={{ flexShrink: 0, marginTop: 1 }}>
        {done ? <CheckCircle2 size={16} color="#16a34a"/> : fail ? <XCircle size={16} color="#dc2626"/> : <Loader2 size={16} style={{ animation: 'spin 1s linear infinite', color: '#2563eb' }}/>}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="truncate" style={{ flex: 1, fontSize: 12, fontWeight: 600, color: '#141413' }}>{t.desc.split('\n')[0]}</span>
          <span style={{ flexShrink: 0, fontSize: 10, fontWeight: 600, padding: '1px 8px', borderRadius: 999,
            background: done ? '#eaf6ec' : fail ? '#fdeaea' : '#eef2ff',
            color: done ? '#16a34a' : fail ? '#dc2626' : '#2563eb' }}>
            {done ? '完成' : fail ? '失败' : '执行中'}
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 3, fontSize: 11, color: '#9a9993' }}>
          {t.route_type && <span>路由 {t.route_type}</span>}
          {t.duration != null && <span>{t.duration}s</span>}
          {t.error && <span style={{ color: '#dc2626' }}>{t.error}</span>}
          {fail && (
            <button onClick={e => { e.stopPropagation(); onRetry(t.id) }}
              style={{ background: 'none', border: 'none', color: '#2563eb', cursor: 'pointer', fontSize: 11, padding: 0 }}><RotateCcw size={11}/> 重试</button>
          )}
        </div>
        {!done && (t.logs || []).length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 6, maxHeight: 120, overflowY: 'auto', background: '#faf9f5', border: '1px solid var(--border)', borderRadius: 6, padding: '6px 8px' }}>
            {(t.logs || []).map((l, i) => (
              <div key={i} style={{ fontSize: 10, fontFamily: 'var(--font-mono)', color: l.kind === 'tool:done' ? '#16a34a' : (l.kind === 'gen' ? '#9a9993' : '#2563eb'), lineHeight: 1.4 }}>{l.msg}</div>
            ))}
          </div>
        )}
        {done && (t.files || []).length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3, marginTop: 6 }}>
            {(t.files || []).map(f => (
              <div key={f} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, fontFamily: 'var(--font-mono)', padding: '3px 8px', background: '#f3f2ec', border: '1px solid var(--border)', borderRadius: 6, color: '#6b6b68', maxWidth: '100%' }}>
                <FileText size={12} color="#9a9993" style={{ flexShrink: 0 }}/>
                <span className="truncate" style={{ flex: 1 }}>{f}</span>
                <button onClick={e => { e.stopPropagation(); onReveal(f) }}
                  title="在文件夹中显示" aria-label="在文件夹中显示"
                  style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 1, display: 'flex', color: '#9a9993', flexShrink: 0 }}>
                  <FolderOpen size={13}/>
                </button>
              </div>
            ))}
            {t.verdict && (() => {
              const c = verdictChip(t.verdict)
              return <span style={{ fontSize: 10, fontWeight: 600, padding: '2px 8px', borderRadius: 6, background: c.bg, color: c.fg, width: 'fit-content' }}>{c.mark} {t.verdict}</span>
            })()}
          </div>
        )}
      </div>
    </div>
  )
})
