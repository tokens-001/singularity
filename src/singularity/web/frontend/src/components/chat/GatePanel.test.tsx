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

/** 取摘要行里那个 `QA：…` 的**颜色** —— `render()` 只回 textContent，颜色断言得走 DOM。 */
function qaSpanColor(acceptance: any): string {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const root = createRoot(el)
  act(() => { root.render(<AcceptancePanel acceptance={acceptance} />) })
  const span = Array.from(el.querySelectorAll('span'))
    .find((s) => (s.textContent || '').startsWith('QA：')) as HTMLElement | undefined
  const color = span?.style.color || ''
  act(() => { root.unmount() })
  el.remove()
  return color
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

  it('QA 报告读不出来时显示"核不了"，不是"通过"', () => {
    // 🔴 这是 `a33bf79` 引入的真洞（2026-09-14 外派反审抓到）：
    // 后端读不出 qa_report.json 时给的占位符 `{"error": …}` 是**真值**，
    // 于是 `!!qa` 成立、`sum` 落成 {}、`failed` 算成 0 ⇒ 渲染成绿色「通过」。
    // **删掉 `qaUnverifiable` 那一行它会红。**
    const t = render(<AcceptancePanel acceptance={{
      qa_report: { error: '报告读不出来（JSONDecodeError）—— 不代表没跑过 QA，先修好文件再看结论' },
    }} />)
    expect(t).not.toContain('QA：通过')
    expect(t).toContain('QA：核不了')
    // 后端那句解释得**露在人审门上** —— 否则它只活在 alerts.jsonl 里，坐在这道门前的人看不到
    expect(t).toContain('不代表没跑过 QA')
  })

  it('报告是个真值但没有 summary ⇒ 一律当核不了', () => {
    // 判据钉的是"**像不像一份报告**"，不是"是不是真值" ——
    // 只判真值的话，换个形状的占位符又会重新爬成绿灯。
    const t = render(<AcceptancePanel acceptance={{ qa_report: { foo: 1 } }} />)
    expect(t).not.toContain('QA：通过')
    expect(t).toContain('QA：核不了')
  })

  it('"核不了"要用琥珀色，不是绿色 —— 与隔壁 conformance 同口径', () => {
    // ⚠️ 光断言文字不够：**变异验证时"只改颜色"那一刀没被抓住**
    // （文字仍是"核不了"，但颜色掉回绿色系就骗人了）。颜色是这条修复的理由本身
    // —— 隔壁 `需求符合性` 的"无法核验"早就是琥珀色，同一个"我核不了"不该一边琥珀一边绿。
    expect(qaSpanColor({ qa_report: { error: 'x' } })).toBe('rgb(180, 83, 9)')   // #b45309 琥珀
    expect(qaSpanColor({ qa_report: { issues: [], summary: { failed: 0, verdict: 'go' } } }))
      .toBe('rgb(22, 163, 74)')                                                  // #16a34a 绿（对照）
  })

  it('项目 issues 要露在折叠框外 —— 验收被跳过不能是静默的', () => {
    const t = render(<AcceptancePanel acceptance={{}} projectIssues={[
      { type: 'verification_skipped', detail: '验收跳过: 架构没产出约束清单, QA/安全审计师都没跑' },
    ]} />)
    expect(t).toContain('验收跳过')
    expect(t).toContain('架构没产出约束清单')
  })
})
