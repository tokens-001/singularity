// @vitest-environment jsdom
/**
 * GATE3 验收摘要渲染测试 —— 锁住"核验不了绝不显示成通过"。
 *
 * 后端在不带产出参数时会如实返回 reason="无法核验 —— 这不是通过"（但 passed=true）。
 * 前端要是只读 passed 渲染个绿色"通过"，就把谎撒出去了 —— 和用量页那个
 * "$0.00 看着可信"是同一类 bug。harness 照抄 lib/usage.test.tsx（裸 createRoot + act）。
 */
import { describe, it, expect } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { AcceptancePanel } from './GatePanel'

function render(node: ReactNode): string {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const root = createRoot(el)
  act(() => { root.render(node) })
  const text = el.textContent || ''
  act(() => { root.unmount() })
  el.remove()
  return text
}

describe('GATE3 验收摘要', () => {
  it('QA 有问题时显示问题数，不是"通过"', () => {
    const t = render(<AcceptancePanel acceptance={{
      qa_report: { issues: [{ severity: 'critical', description: '示例' }],
                   summary: { failed: 1, verdict: 'no_go' } },
    }} />)
    expect(t).toContain('1 个问题')
    expect(t).not.toContain('QA：通过')
  })

  it('QA 干净才显示通过', () => {
    const t = render(<AcceptancePanel acceptance={{
      qa_report: { issues: [], summary: { failed: 0, verdict: 'go' } },
    }} />)
    expect(t).toContain('QA：通过')
  })

  it('符合性无法核验时绝不显示"通过"', () => {
    const t = render(<AcceptancePanel acceptance={{
      qa_report: null,
      conformance: { passed: true, reason: '无法核验 —— 这不是通过',
                     evidence: { unverifiable: true } },
    }} />)
    expect(t).toContain('无法核验')
    expect(t).not.toContain('需求符合性：通过')
  })

  it('没有 QA 报告时如实说没有，不谎报通过', () => {
    const t = render(<AcceptancePanel acceptance={{}} />)
    expect(t).toContain('QA：无报告')
    expect(t).not.toContain('QA：通过')
  })

  it('项目 issues 要露在折叠框外 —— 验收被跳过不能是静默的', () => {
    const t = render(<AcceptancePanel acceptance={{}} projectIssues={[
      { type: 'verification_skipped', detail: '验收跳过: 架构没产出约束清单, QA/安全审计师都没跑' },
    ]} />)
    expect(t).toContain('验收跳过')
    expect(t).toContain('架构没产出约束清单')
  })
})
