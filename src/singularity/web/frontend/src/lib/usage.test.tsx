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

vi.mock('./api', () => ({
  api: { tokenUsage: () => Promise.resolve(FIXTURE) },
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
