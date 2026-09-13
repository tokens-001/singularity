// @vitest-environment jsdom
/**
 * SSE **断了要自己重连** —— 不能等下一次挂载。
 *
 * ⚠️ `EventSource` 只在"可重试的错误"上自动重连（网络抖动那种）；
 * 拿到非 2xx（例如撞上后端 20 连接上限的 503）或服务重启之后，它进 CLOSED **并放弃**。
 * 而 `_ensureEs()` 原来**只在 `useSSE` 挂载时被调一次** ⇒ 页面开着时断了就永远不回来
 * （2026-09-14，外派④扫前端抓出）。
 *
 * 这条测"重建"这个行为：驱动一次 `onerror`（readyState=CLOSED）后，
 * **到点必须看到一个新的 EventSource**。
 */
import { describe, it, expect, beforeAll, afterEach, vi } from 'vitest'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { useSSE } from './useSSE'

class FakeES {
  static instances: FakeES[] = []
  static CONNECTING = 0
  static OPEN = 1
  static CLOSED = 2
  readyState = 1
  onopen: any = null
  onmessage: any = null
  onerror: any = null
  closed = false
  constructor(public url: string) { FakeES.instances.push(this) }
  close() { this.closed = true; this.readyState = 2 }
}

beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
  g.EventSource = FakeES as any
})

afterEach(() => { vi.useRealTimers() })

function mount() {
  function Probe() { useSSE(() => {}); return null }
  const el = document.createElement('div')
  act(() => { createRoot(el).render(<Probe />) })
}

describe('SSE 断线重连', () => {
  it('连接进 CLOSED 之后，到点会重建一条', async () => {
    vi.useFakeTimers()
    const before = FakeES.instances.length
    mount()
    expect(FakeES.instances.length, '挂载时该先建一条').toBe(before + 1)

    const es = FakeES.instances[FakeES.instances.length - 1]
    es.readyState = FakeES.CLOSED          // 浏览器放弃重连的那种状态
    await act(async () => { es.onerror?.(new Event('error')) })

    await act(async () => { vi.advanceTimersByTime(6000) })

    expect(FakeES.instances.length, '到点没重建 —— 页面开着时断了就永远不回来').toBe(before + 2)
  })
})
