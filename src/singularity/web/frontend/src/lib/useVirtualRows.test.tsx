// @vitest-environment jsdom
// 窗口化的切片算术错了会直接表现为"滚动时行错位/丢失"，所以这里把数字钉死。
import { describe, it, expect, beforeAll } from 'vitest'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { useVirtualRows } from './useVirtualRows'

const roCallbacks: Array<() => void> = []
beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
  g.ResizeObserver = class { constructor(cb: () => void) { roCallbacks.push(cb) } observe() {} unobserve() {} disconnect() {} }
})

let last: any
function Probe({ total }: { total: number }) {
  last = useVirtualRows(total, 40, 0, 2)   // 行高 40、无间距、overscan 2
  return <div ref={last.ref} onScroll={last.onScroll} />
}

function mount(total: number) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  act(() => { createRoot(el).render(<Probe total={total} />) })
  return el.querySelector('div') as HTMLDivElement
}

describe('useVirtualRows', () => {
  it('列表比一屏短时全部渲染，占位为 0', () => {
    const box = mount(3)
    Object.defineProperty(box, 'clientHeight', { value: 400, configurable: true })
    act(() => { roCallbacks.forEach(cb => cb()) })
    expect(last.start).toBe(0)
    expect(last.end).toBe(3)
    expect(last.padTop).toBe(0)
    expect(last.padBottom).toBe(0)
  })

  it('滚动到中段只渲染可视区 + overscan，占位撑住滚动条', () => {
    const box = mount(1000)
    Object.defineProperty(box, 'clientHeight', { value: 400, configurable: true })
    act(() => { roCallbacks.forEach(cb => cb()) })
    box.scrollTop = 800
    act(() => { last.onScroll({ currentTarget: box }) })
    // floor(800/40)=20，减 overscan 2 → 18；一屏 400/40=10 行 + 两侧 overscan 4 → 32
    expect(last.start).toBe(18)
    expect(last.end).toBe(32)
    expect(last.padTop).toBe(720)          // 18 * 40
    expect(last.padBottom).toBe(38720)     // (1000 - 32) * 40
  })
})
