// @vitest-environment jsdom
/**
 * `useRun` 的返回契约 —— **成功要把响应带回来**。
 *
 * ⚠️ 原来返回 `Promise<boolean>`：`const res = await run(...)` 拿到的是 `true`，
 * 于是 `res?.suggested_flow` 永远 undefined —— 后端在 200 里回的
 * "看着像小活，要不要走轻量流程"（§47 的人审建议）**是死功能**
 * （2026-09-14，外派④扫前端时对比同仓两种写法抓出）。
 *
 * 契约：**成功 → 响应本身**（没有响应体时退回 `true`）；**失败 → `false`**。
 * 这条要钉住，否则下一个人把返回值改回 boolean，"死功能"会**无声地回来**。
 */
import { describe, it, expect, beforeAll } from 'vitest'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { App as AntApp } from 'antd'
import { useRun } from './toast'

beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
  g.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})

/** 把 hook 的返回值捞出来。 */
async function callRun(fn: () => Promise<unknown>): Promise<unknown> {
  let got: unknown = 'NEVER_CALLED'
  function Probe() { const run = useRun(); ;(globalThis as any).__run = run; return null }
  const el = document.createElement('div')
  await act(async () => { createRoot(el).render(<AntApp><Probe /></AntApp>) })
  await act(async () => { got = await (globalThis as any).__run(fn) })
  return got
}

describe('useRun 的返回契约', () => {
  it('成功时把**响应**带回来（不只是 true）', async () => {
    const payload = { ok: true, suggested_flow: { reason: '看着像小活' }, project: { id: 'p1' } }
    const got: any = await callRun(() => Promise.resolve(payload))
    expect(got?.suggested_flow?.reason, '响应被吞了 —— 依赖它的功能会变死功能').toBe('看着像小活')
  })

  it('成功但没有响应体时退回 true（既有 `if (!(await run(...)))` 才不会被误判成失败）', async () => {
    expect(await callRun(() => Promise.resolve(undefined))).toBe(true)
  })

  it('失败时返回 false（调用方的早退判据靠它）', async () => {
    expect(await callRun(() => Promise.reject(new Error('boom')))).toBe(false)
  })
})
