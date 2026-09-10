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

/** 一个已定价 + 一个未定价的模型。后者是防造假断言的主角。 */
const HISTORY = {
  range: '30d',
  earliest: '2026-08-13',
  days: [],
  models: [
    { model: 'priced-model', tokens: 1_000_000, share: 0.3333, cost: 0.28, price: 0.28 },
    { model: 'unpriced-model', tokens: 2_000_000, share: 0.6667, cost: null, price: null },
  ],
  totals: { tokens: 3_000_000, tasks: 9, active_days: 1, cost: 0.28,
            unpriced_models: ['unpriced-model'] },
  activity: {},
}

/** 一个模型都没配单价 —— 合计**没有数可报**，必须显示"未配置价格"而不是 $0.0000。 */
const HISTORY_NONE_PRICED = {
  ...HISTORY,
  models: [{ model: 'unpriced-model', tokens: 3_000_000, share: 1, cost: null, price: null }],
  totals: { ...HISTORY.totals, cost: 0, unpriced_models: ['unpriced-model'] },
}

const EMPTY = {
  range: '7d', earliest: '2026-09-05', days: [],
  models: [],
  totals: { tokens: 0, tasks: 0, active_days: 0, cost: 0, unpriced_models: [] },
  activity: {},
}

let current = HISTORY
const calls: string[] = []

vi.mock('./api', () => ({
  api: {
    usageHistory: (r: string) => { calls.push(r); return Promise.resolve(current) },
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
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  return el
}

describe('用量页', () => {
  it('未配置单价的模型显示"未配置价格"，绝不显示 $0.00', async () => {
    current = HISTORY
    const el = await render(<Usage />)
    const text = el.textContent || ''

    expect(text).toContain('未配置价格')
    expect(text).not.toContain('$0.00')
    expect(text).not.toContain('$0.0000')
  })

  it('已定价的模型显示真实费用和单价', async () => {
    current = HISTORY
    const el = await render(<Usage />)
    const text = el.textContent || ''

    // 1,000,000 tokens × $0.28/M = $0.28
    expect(text).toContain('$0.2800')
    expect(text).toContain('$0.28/M')
  })

  it('占比照实显示', async () => {
    current = HISTORY
    const el = await render(<Usage />)
    const text = el.textContent || ''
    expect(text).toContain('33%')
    expect(text).toContain('67%')
  })

  it('一个模型都没配单价时，合计也显示"未配置价格"而不是 $0.0000', async () => {
    current = HISTORY_NONE_PRICED
    const el = await render(<Usage />)
    const text = el.textContent || ''
    expect(text).toContain('未配置价格')
    expect(text).not.toContain('$0.0000')
    expect(text).not.toContain('$0.00')
  })

  it('未定价时提示里有"未配置单价"说明和去配置入口', async () => {
    current = HISTORY
    const el = await render(<Usage />)
    const text = el.textContent || ''
    expect(text).toContain('unpriced-model')
    expect(text).toContain('未配置单价')
    expect(text).toContain('去配置')
  })

  it('显示统计起始日 —— 截断过的"全部"必须标注，否则是在撒谎', async () => {
    current = HISTORY
    const el = await render(<Usage />)
    expect(el.textContent).toContain('统计自 2026-08-13')
  })

  it('空数据渲染空状态，不出现 NaN', async () => {
    current = EMPTY
    const el = await render(<Usage />)
    expect(el.textContent).toContain('这段时间暂无用量')
    expect(el.textContent).not.toContain('NaN')
    const bad = Array.from(el.querySelectorAll('*'))
      .map(n => n.getAttribute('style') || '')
      .filter(s => s.includes('NaN') || s.includes('Infinity'))
    expect(bad, `样式里出现 NaN/Infinity: ${bad.join(' | ')}`).toEqual([])
  })

  it('切时间段会用对的 query 重新取数', async () => {
    current = HISTORY
    calls.length = 0
    const el = await render(<Usage />)
    expect(calls).toContain('30d')          // 默认范围

    const btn = Array.from(el.querySelectorAll('button'))
      .find(b => b.textContent === '近7天') as HTMLElement
    expect(btn, '没找到"近7天"按钮').toBeTruthy()
    await act(async () => { btn.click() })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(calls).toContain('7d')
  })
})
