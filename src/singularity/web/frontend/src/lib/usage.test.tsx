// @vitest-environment jsdom
/**
 * 用量页渲染测试 —— 锁住"未配置价格绝不显示成 $0.00"。
 *
 * 这条就是能抓住原始 bug 的那种测试：老代码里后端对没配单价的模型返回 0，
 * 前端 `(m.cost || 0).toFixed(2)` 渲染成看着可信的 $0.00 —— 整个界面的金额都是编的。
 *
 * harness 照抄 lib/antd-smoke.test.tsx（本项目没有 @testing-library/react，
 * 用裸 createRoot + act，jsdom 缺 ResizeObserver 要补桩）。
 */
import { describe, it, expect, beforeAll, vi } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { App as AntApp } from 'antd'

const FIXTURE = {
  daily_tokens: 3_000_000,
  daily_cost: 0.28,          // 只算了已定价的那个模型
  budget_daily: 5,
  budget_monthly: 100,
  warning: '',
  by_project: [],
  by_level: {},
  unpriced_models: ['unpriced-model'],
  by_model: [
    { model: 'priced-model', tokens: 1_000_000, cost: 0.28, tasks: 3, share: 0.3333, price: 0.28 },
    { model: 'unpriced-model', tokens: 2_000_000, cost: null, tasks: 6, share: 0.6667, price: null },
  ],
  by_model_level: [],
}

/** 2026-09-11 往前 n 天的本地日期串（不能用 toISOString —— 那是 UTC，会串天）。 */
function dayStr(offsetFromLast: number, n: number) {
  const d = new Date(2026, 8, 11 - (n - 1 - offsetFromLast))
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** 历史 fixture：只有最后一天有量，且那个模型未配单价。 */
function histFixture(range: string) {
  const n = range === '7d' ? 7 : 30
  const days = Array.from({ length: n }, (_, i) => ({
    date: dayStr(i, n),
    tokens: i === n - 1 ? 3_000_000 : 0,
    tasks: i === n - 1 ? 9 : 0,
    elapsed_s: 0,
  }))
  const peak = days[n - 1].date
  return Promise.resolve({
    range, earliest: days[0].date, days,
    models: [{ model: 'unpriced-model', tokens: 3_000_000, share: 1, cost: null, price: null }],
    totals: { tokens: 3_000_000, tasks: 9, active_days: 1, cost: 0,
              unpriced_models: ['unpriced-model'] },
    activity: { peak_day: { date: peak, tokens: 3_000_000 }, peak_hour: 9,
                current_streak: 1, longest_streak: 1, elapsed_s: 0, max_elapsed_s: 0 },
  })
}

vi.mock('./api', () => ({
  api: {
    tokenUsage: () => Promise.resolve(FIXTURE),
    // 必须提供 —— Usage.tsx 会调它。缺了虽然被页面 try/catch 兜住不炸，
    // 但那样测试跑的是"历史区根本没渲染"的退化路径，断言不到新代码。
    usageHistory: (r: string) => histFixture(r),
  },
}))

import Usage from '../pages/Usage'

beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
  g.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})

async function render(node: ReactNode) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  await act(async () => { createRoot(el).render(<MemoryRouter><AntApp>{node}</AntApp></MemoryRouter>) })
  // 冲掉 tokenUsage 的 promise + 一轮 setState
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  return el
}

describe('用量页', () => {
  it('未配置单价的模型显示"未配置价格"，绝不显示 $0.00', async () => {
    const el = await render(<Usage />)
    const text = el.textContent || ''

    expect(text).toContain('未配置价格')
    expect(text).not.toContain('$0.00')
  })

  it('已定价的模型显示真实费用，并按单价算得出来', async () => {
    const el = await render(<Usage />)
    const text = el.textContent || ''

    // 1,000,000 tokens × $0.28/M = $0.28
    expect(text).toContain('$0.2800')
    expect(text).toContain('$0.28/M')
  })

  it('总额带 "+" 并在未定价时有警示提示', async () => {
    const el = await render(<Usage />)
    const text = el.textContent || ''

    // 有模型没配单价 → daily_cost 只是下限，必须标出来
    expect(text).toContain('$0.2800+')
    expect(text).toContain('unpriced-model')
    expect(text).toContain('未配置单价')
  })

  it('占比和任务数照实显示', async () => {
    const el = await render(<Usage />)
    const text = el.textContent || ''

    expect(text).toContain('33%')
    expect(text).toContain('67%')
  })
})

describe('用量页 — 历史区（ZCode 式统计）', () => {
  it('热力图格子数与范围一致，且带 data-day 钩子', async () => {
    const el = await render(<Usage />)
    const cells = el.querySelectorAll('[data-day]')
    // 7 或 30 天范围由默认 range=30d 决定；核心是"格子数 == 天数"，不空不重
    expect(cells.length).toBeGreaterThanOrEqual(30)
    const peak = Array.from(cells).find(c => c.getAttribute('data-day') === '2026-09-11')
    expect(peak, '峰值那天必须有格子').toBeTruthy()
    expect(peak!.getAttribute('data-tokens')).toBe('3000000')
  })

  it('峰值日的趋势柱被标出来（用属性断言，jsdom 不做布局）', async () => {
    const el = await render(<Usage />)
    const peaks = el.querySelectorAll('[data-peak="1"]')
    // 热力图 + 趋势图各一处（同一天既是峰值格也是峰值柱时至少一处）
    expect(peaks.length).toBeGreaterThanOrEqual(1)
    expect(Array.from(peaks).some(p => p.getAttribute('data-day') === '2026-09-11')).toBe(true)
  })

  it('历史区的未定价模型显示"未配置价格"，且页面不出现 $0.00', async () => {
    const el = await render(<Usage />)
    const text = el.textContent || ''
    expect(text).toContain('未配置价格')
    expect(text).not.toContain('$0.00')
  })

  it('显示统计起始日 —— 截断过的"全部"必须标注，否则是在撒谎', async () => {
    const el = await render(<Usage />)
    // 默认范围是 30 天，起始日 = fixture 里第一天，不是峰值那天
    expect(el.textContent).toContain(`统计自 ${dayStr(0, 30)}`)
  })

  it('活跃度指标渲染出来，且无 NaN/Infinity', async () => {
    const el = await render(<Usage />)
    const text = el.textContent || ''
    expect(text).toContain('活跃度')
    expect(text).toContain('高峰时段')
    expect(text).toContain('09:00–10:00')
    expect(text).not.toContain('NaN')
    expect(text).not.toContain('Infinity')
  })

  it('全零范围渲染空状态，不吐出 NaN% 高度', async () => {
    const zero = {
      range: '7d', earliest: '2026-09-05',
      days: Array.from({ length: 7 }, (_, i) => ({
        date: dayStr(i, 7), tokens: 0, tasks: 0, elapsed_s: 0 })),
      models: [],
      totals: { tokens: 0, tasks: 0, active_days: 0, cost: 0, unpriced_models: [] },
      activity: { peak_day: null, peak_hour: null, current_streak: 0,
                  longest_streak: 0, elapsed_s: 0, max_elapsed_s: 0 },
    }
    const { api } = await import('./api')
    const spy = vi.spyOn(api, 'usageHistory').mockResolvedValue(zero as any)
    const el = await render(<Usage />)
    expect(el.textContent || '').toContain('这段时间还没有用量')
    // ⚠️ 光查 textContent 抓不到这个 bug：NaN 会落在 style 属性里（height: NaN%），
    // 文本里根本没有它。必须逐个看 style。
    const bad = Array.from(el.querySelectorAll('*'))
      .map(n => n.getAttribute('style') || '')
      .filter(s => s.includes('NaN') || s.includes('Infinity'))
    expect(bad, `样式里出现了 NaN/Infinity 高度: ${bad.join(' | ')}`).toEqual([])
    spy.mockRestore()
  })
})
