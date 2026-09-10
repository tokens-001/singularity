// @vitest-environment jsdom
/**
 * 侧边栏预算条 —— 锁住"没配预算就不许显示百分比"。
 *
 * 这是本段最重要的一条防造假规则：`budget_daily` 默认是 0（没配），
 * 若退回 `0% used / 剩余 $0.00 / 总额 $0.00`，等于**编了一个用户从没设过的套餐** ——
 * 和之前那个假的 $0.19 是同一类错。
 */
import { describe, it, expect, beforeAll } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { BudgetMeter } from '../components/AppLayout'

beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
  g.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})

async function render(node: ReactNode) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  await act(async () => { createRoot(el).render(node) })
  return el
}

describe('侧边栏预算条', () => {
  it('没配预算时什么都不渲染 —— 不编百分比，也不编 $0.00', async () => {
    const el = await render(<BudgetMeter used={0} budget={0} unpriced={[]} />)
    expect(el.textContent).toBe('')
    expect(el.querySelectorAll('*').length).toBe(0)
  })

  it('配了预算才显示百分比 / 剩余 / 总额 / 重置说明', async () => {
    const el = await render(<BudgetMeter used={2} budget={5} unpriced={[]} />)
    const t = el.textContent || ''
    expect(t).toContain('40% used')
    expect(t).toContain('剩余 $3.00')
    expect(t).toContain('总额 $5.00')
    expect(t).toContain('每日 00:00 重置')
  })

  it('有模型没配单价时读数是下限，显示 ≥', async () => {
    const el = await render(<BudgetMeter used={2} budget={5} unpriced={['m']} />)
    expect(el.textContent).toContain('≥40% used')
  })

  it('超预算不夹到 0，如实显示负数剩余（藏成 $0.00 是反方向的同一种撒谎）', async () => {
    const el = await render(<BudgetMeter used={7} budget={5} unpriced={[]} />)
    const t = el.textContent || ''
    expect(t).toContain('140% used')          // 数字不夹
    expect(t).toContain('剩余 $-2.00')        // 也不夹
    // 夹的是条本身
    const bar = el.querySelectorAll('div[style*="width"]')[0] as HTMLElement
    expect(bar.style.width).toBe('100%')
  })

  it('百分比不出现 NaN', async () => {
    const el = await render(<BudgetMeter used={0} budget={5} unpriced={[]} />)
    expect(el.textContent).toContain('0% used')
    expect(el.textContent).not.toContain('NaN')
  })
})
