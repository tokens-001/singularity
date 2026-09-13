// @vitest-environment jsdom
/**
 * 任务卡的验收结论渲染 —— 锁住"**没通过绝不显示成对勾**"。
 *
 * 原来的判据是 `t.verdict && t.verdict !== '?'`，样式写死绿色 + `✓`。
 * 而后端 `verdict` 的取值是 `validator.py` 里那几个
 * （`通过` / `阻断` / `gate失败` / `信息不足` / `人工复核` / `未知`）
 * ⇒ 任务**被阻断**、门禁**失败**时，卡片上是绿色的「✓ 阻断」。
 * 判据落在"非空"而不是"成功"上，默认那一侧是好消息 ——
 * **和 `GatePanel` 那条 GATE3 绿灯是同一个病**（见 `GatePanel.test.tsx`）。
 *
 * harness 照抄 `GatePanel.test.tsx`（裸 createRoot + act）。
 */
import { describe, it, expect } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { TaskCard } from './TaskCard'

function render(node: ReactNode): string {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const root = createRoot(el)
  act(() => { root.render(node) })
  const text = el.textContent || ''
  act(() => { root.unmount() })
  return text
}

const base = { id: 't1', desc: '一个任务', status: 'done', ts: 0, files: ['a.ts'] }
const noop = () => {}

function card(verdict: string) {
  return render(<TaskCard t={{ ...base, verdict }} onRetry={noop} onReveal={noop} />)
}

describe('验收结论的三态渲染', () => {
  it('「阻断」不许渲染成对勾', () => {
    const text = card('阻断')
    expect(text).toContain('阻断')
    expect(text, '被阻断的任务渲染成了绿色的对勾 —— 判据又落回"非空"了').not.toContain('✓ 阻断')
  })

  it('「gate失败」不许渲染成对勾', () => {
    expect(card('gate失败')).not.toContain('✓ gate失败')
  })

  it('「通过」才是对勾', () => {
    expect(card('通过')).toContain('✓ 通过')
  })

  it('「人工复核」「信息不足」走"待人工"，不是对勾', () => {
    expect(card('人工复核')).not.toContain('✓')
    expect(card('信息不足')).not.toContain('✓')
  })

  it('**不认识的档也不许落到绿** —— 将来后端加了新 verdict，默认那侧必须不是"通过"', () => {
    expect(card('某个以后才有的档')).not.toContain('✓')
  })

  it('没结论时不渲染（灰 = 没数据）', () => {
    const text = card('')
    expect(text).not.toContain('✓')
    expect(text).not.toContain('✕')
  })
})
