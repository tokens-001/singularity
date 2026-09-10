import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { useRun, useToast } from '../lib/toast'
import { Save } from 'lucide-react'

const TIERS: [string, string][] = [
  ['custom', '架构融合'],
]

/** Fusion 配置：两阶段合成（五维差异分析 → 定稿）用哪两个模型。
 *
 * 只有 [custom] 一段 —— dual/triple/super 三档火力是历史残留，唯一的读入口
 * fuse_outputs() 全仓库零调用，从未执行过，已随配置一并删除。
 * 下拉只列「激活」模型（provider 已配置且有 key）—— 其余模型后端解析不出
 * base_url/key，调用会静默返回空、融合退化成第一个模型的初稿。
 */
export default function FusionTab() {
  const [cfg, setCfg] = useState<any>(null)
  const [models, setModels] = useState<any[]>([])
  const [saving, setSaving] = useState(false)
  const run = useRun()
  const toast = useToast()

  const load = () => {
    api.fusionConfig().then(setCfg).catch(() => toast('加载 Fusion 配置失败', 'error'))
    api.models().then((ms: any[]) => setModels(ms.filter(m => m.api_available)))
      .catch(() => toast('加载模型列表失败', 'error'))
  }
  useEffect(() => { load() }, [])

  const set = (tier: string, field: string, v: string) =>
    setCfg((c: any) => ({ ...c, [tier]: { ...(c[tier] || {}), [field]: v } }))

  const save = async () => {
    setSaving(true)
    const payload: any = {}
    TIERS.forEach(([t]) => { if (cfg[t]) payload[t] = cfg[t] })
    const ok = await run(() => api.updateFusionConfig(payload), '已保存')
    setSaving(false)
    if (ok) load()
  }

  // 当前值不在激活列表里时也列出来（标注未激活），免得保存时被静默改掉
  const options = (cur: string) => {
    const ids = models.map(m => m.id)
    return cur && !ids.includes(cur) ? [cur, ...ids] : ids
  }
  const labelOf = (id: string) =>
    models.find(m => m.id === id)?.display || (id ? `${id}（未激活）` : '')

  if (!cfg) return <div className="fs-11 text-muted" style={{ padding: 20, textAlign: 'center' }}>加载中...</div>

  return (
    <div>
      <div className="flex-center gap-8" style={{ marginBottom: 10 }}>
        <span className="fw-600 fs-12 text-secondary">融合模型</span>
        <span className="fs-10 text-muted">两阶段合成用「裁判 + 定稿」；v2 机制用「提取员」，定稿人按历史范围纪律自动选 —— 所以 v2 下这里的「定稿」不生效</span>
        <span className="flex-1"/>
        <button onClick={save} disabled={saving} className="btn-sm"><Save size={12}/> 保存</button>
      </div>
      {TIERS.map(([tier, label]) => {
        const t = cfg[tier] || {}
        return (
          <div key={tier} className="flex-center gap-8"
            style={{ marginBottom: 6, padding: '8px 12px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
            <span className="fw-600 fs-11" style={{ width: 190, flexShrink: 0 }}>{label}</span>
            <label className="fs-10 text-muted" style={{ flexShrink: 0 }}>裁判</label>
            <select className="inp-dark" style={{ flex: 1 }} value={t.judge_model || ''}
              aria-label={`${tier} 裁判模型`}
              onChange={e => set(tier, 'judge_model', e.target.value)}>
              <option value="">（未设置）</option>
              {options(t.judge_model).map(id => <option key={id} value={id}>{labelOf(id)}</option>)}
            </select>
            <label className="fs-10 text-muted" style={{ flexShrink: 0 }}>定稿</label>
            <select className="inp-dark" style={{ flex: 1 }} value={t.call_model || ''}
              aria-label={`${tier} 定稿模型`}
              onChange={e => set(tier, 'call_model', e.target.value)}>
              <option value="">（未设置）</option>
              {options(t.call_model).map(id => <option key={id} value={id}>{labelOf(id)}</option>)}
            </select>
            {/* v2 的提取员。原来这栏 UI 上根本不存在 —— _v2_extractor_model() 一直
                在读配置，用户却看不到也改不了，只能用代码里的默认值。 */}
            <label className="fs-10 text-muted" style={{ flexShrink: 0 }}>提取员</label>
            <select className="inp-dark" style={{ flex: 1 }} value={t.extract_model || ''}
              aria-label={`${tier} 提取员模型`}
              onChange={e => set(tier, 'extract_model', e.target.value)}>
              <option value="">（默认）</option>
              {options(t.extract_model).map(id => <option key={id} value={id}>{labelOf(id)}</option>)}
            </select>
          </div>
        )
      })}
      <div className="fs-10 text-muted" style={{ marginTop: 10, lineHeight: 1.8 }}>
        下拉里是当前激活的模型（{models.length} 个）。保存后立即生效，无需重启。
        标「未激活」的是配置里已有、但对应 provider 没配好 key 的模型 —— 选它该阶段会静默失败。
      </div>
    </div>
  )
}
