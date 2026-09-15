// @vitest-environment jsdom
/**
 * 页面出错时的**出路**（2026-09-15 真机：用户被卡在一个永远好不了的「重试」上）。
 *
 * 现场：`npm run build` 之后懒加载路由的 chunk 换了文件名，**已打开的旧页面**去要旧的
 * ⇒ 404 ⇒ 浏览器抛 `Importing a module script failed.` ⇒ ErrorBoundary 显示
 * "页面出错了 + 重试"。而那个文件是真没了，**再渲染一百次还是同一个 404** ——
 * 用户没有任何出路（唯一的解法"刷新"当时根本不在屏幕上）。
 *
 * ⚠️ 判据的核心是「**刷新这个出路永远在**」，不是"认不认得那句话" ——
 * 认不出时只是顺序不同，不能变成"没有刷新按钮"。
 */
import { describe, it, expect, beforeAll, vi, beforeEach } from 'vitest'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { ErrorBoundary } from './ErrorBoundary'

beforeAll(() => {
  ;(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true
})

beforeEach(() => { vi.restoreAllMocks() })

/** 渲染一个"一进来就抛"的孩子，看边界把什么画到了屏幕上 */
async function renderWithError(err: Error): Promise<HTMLElement> {
  function Boom(): never { throw err }
  const el = document.createElement('div')
  document.body.appendChild(el)
  // React 会把边界捕获的错误也往 console.error 打一份，这里静音掉免得刷屏
  const quiet = vi.spyOn(console, 'error').mockImplementation(() => {})
  await act(async () => { createRoot(el).render(<ErrorBoundary><Boom /></ErrorBoundary>) })
  quiet.mockRestore()
  return el
}

const textOf = (el: HTMLElement) => el.textContent || ''
const buttons = (el: HTMLElement) =>
  Array.from(el.querySelectorAll('button')).map(b => (b.textContent || '').trim())

describe('ErrorBoundary 的出路', () => {
  it('chunk 加载失败 → 说"版本过期"，并把「刷新页面」摆出来', async () => {
    const el = await renderWithError(
      new TypeError('Importing a module script failed.'))

    expect(textOf(el)).toContain('页面版本过期了')
    expect(textOf(el), '得说清"刷新就好"，不然用户只能瞎点').toContain('刷新')
    expect(buttons(el), '「刷新页面」得在').toContain('刷新页面')
    // 认出来了 → 刷新排第一（重试救不了这个场景，别让用户先点到它）
    expect(buttons(el)[0]).toBe('刷新页面')
  })

  it('普通报错 → 仍是"页面出错了"，但刷新这条出路**也在**', async () => {
    const el = await renderWithError(new Error('Cannot read properties of undefined'))

    expect(textOf(el)).toContain('页面出错了')
    expect(textOf(el), '普通错误不该误报成"版本过期"').not.toContain('页面版本过期了')
    expect(buttons(el), '**任何**错误下都得有刷新这条出路').toContain('刷新页面')
    expect(buttons(el)[0], '普通错误该先给重试').toBe('重试')
  })

  it('几种浏览器/打包器的说法都认得出来', async () => {
    for (const msg of [
      'Failed to fetch dynamically imported module',
      'error loading dynamically imported module',
      'ChunkLoadError: Loading chunk 3 failed',
    ]) {
      const el = await renderWithError(new Error(msg))
      expect(textOf(el), `认不出这条：${msg}`).toContain('页面版本过期了')
      el.remove()
    }
  })

  it('点「刷新页面」真的调 location.reload', async () => {
    const reload = vi.fn()
    const orig = window.location
    // jsdom 的 location.reload 不可直接 spy，换个可写对象顶上
    Object.defineProperty(window, 'location', { configurable: true, value: { ...orig, reload } })

    const el = await renderWithError(new TypeError('Importing a module script failed.'))
    const btn = Array.from(el.querySelectorAll('button'))
      .find(b => (b.textContent || '').includes('刷新页面')) as HTMLButtonElement
    await act(async () => { btn.click() })

    expect(reload, '按钮没真的刷新 —— 那还是没出路').toHaveBeenCalled()
    Object.defineProperty(window, 'location', { configurable: true, value: orig })
  })
})
