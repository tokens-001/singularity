// @vitest-environment jsdom
// useSSE 的事件过滤 + 合并是这个前端最吃性能的一段：任务执行时 tool:* 事件很密，
// 每个事件都触发一次全量重取会打爆后端。这里用假 EventSource 真跑一遍。
import { describe, it, expect, beforeAll, beforeEach } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { useSSE } from './useSSE'

let lastES: any
beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
  g.EventSource = class { onmessage: any; onerror: any; constructor() { lastES = this } }
})

let root: Root | null = null
beforeEach(() => { if (root) { act(() => root!.unmount()); root = null } })

function mount(kinds: string[] | undefined, onEvent: (e: any) => void) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  root = createRoot(el)
  act(() => { root!.render(<Harness kinds={kinds} onEvent={onEvent} />) })
}

function Harness({ kinds, onEvent }: { kinds?: string[]; onEvent: (e: any) => void }) {
  useSSE(onEvent, { kinds, debounceMs: 30 })
  return null
}

function emit(evt: any) { act(() => { lastES.onmessage({ data: JSON.stringify(evt) }) }) }
function wait(ms: number) { return act(async () => { await new Promise((r) => setTimeout(r, ms)) }) }

describe('useSSE', () => {
  it('只把关心的 kind 交给回调', async () => {
    const seen: any[] = []
    mount(['task'], (e) => seen.push(e))
    emit({ kind: 'tool:start', msg: 'ignored' })
    emit({ kind: 'task', msg: 'kept' })
    await wait(60)
    expect(seen).toHaveLength(1)
    expect(seen[0].msg).toBe('kept')
  })

  it('连发同类事件被合并成一次回调', async () => {
    const seen: any[] = []
    mount(['task'], (e) => seen.push(e))
    emit({ kind: 'task', msg: 'a' })
    emit({ kind: 'task', msg: 'b' })
    emit({ kind: 'task', msg: 'c' })
    await wait(60)
    expect(seen).toHaveLength(1)
    expect(seen[0].msg).toBe('c')   // 只处理最后一次
  })

  it('不传 kinds 时全部事件都放行', async () => {
    const seen: any[] = []
    mount(undefined, (e) => seen.push(e))
    emit({ kind: 'anything' })
    await wait(60)
    expect(seen).toHaveLength(1)
  })
})
