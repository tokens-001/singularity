// @vitest-environment jsdom
/**
 * 项目页「看着像小活，要不要走轻量流程」那条提示的**接线测试**（2026-09-15 补）。
 *
 * 为什么单独补这一条：这条链的**两头都有测试、中间没人守** ——
 * · 后端真回 `suggested_flow`（`_api_projects.project_create`）；
 * · `useRun` 真把响应带出来（`lib/useRun.test.tsx` 就守着这句，怕它又被吞）；
 * · **但没有任何测试碰过 `Projects.tsx`**（全仓 12 个前端测试文件，一个都没有）。
 * ⇒ 把 `{flowHint && …}` 那一整块删掉、或者把采纳调错参数，**全仓不会有一条红**。
 *   （这正是"只测函数不测接线"：§47 那条建议是**靠这一页活着的**，它死了没人知道。）
 *
 * 这里用**真的 `useRun` / `useToast`**（只 mock `api`），所以"响应 → state → 渲染 →
 * 点采纳 → 调接口"整条链都是真的，没有替身会被真实类型甩下。
 *
 * harness 照抄 `pages/Alerts.test.tsx` + `lib/antd-smoke.test.tsx`（裸 createRoot + act，
 * 外层套 ConfigProvider/AntApp —— `useToast` 要 `App.useApp()`）。
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { App as AntApp, ConfigProvider } from 'antd'
import { antdTheme } from '../lib/theme'

vi.mock('../lib/api', () => ({
  api: {
    projects: vi.fn(), project: vi.fn(), projectFiles: vi.fn(),
    createProject: vi.fn(), setFlowWeight: vi.fn(), deleteProject: vi.fn(),
    projectDiff: vi.fn(), projectCost: vi.fn(), snapshotProject: vi.fn(),
  },
}))
vi.mock('../lib/useSSE', () => ({ useSSE: () => {}, useSSEConnected: () => true }))
vi.mock('react-router-dom', () => ({ useNavigate: () => () => {} }))

import { api } from '../lib/api'
import Projects from './Projects'

beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
  g.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => { vi.clearAllMocks() })

/** React 受控 input：直接改 .value 不会触发 onChange，得走原生 setter + input 事件 */
function typeInto(el: HTMLInputElement, v: string) {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  set.call(el, v)
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

async function renderProjects(): Promise<HTMLElement> {
  ;(api.projects as any).mockResolvedValue([])
  const el = document.createElement('div')
  document.body.appendChild(el)
  await act(async () => {
    createRoot(el).render(
      <ConfigProvider theme={antdTheme}><AntApp><Projects /></AntApp></ConfigProvider> as ReactNode)
  })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  return el
}

const byText = (el: HTMLElement, t: string) =>
  Array.from(el.querySelectorAll('button')).find(b => (b.textContent || '').includes(t)) as HTMLButtonElement

/** 走完整条"在界面上建项目"：点新建 → 填表 → 点创建 */
async function createThroughUI(el: HTMLElement, res: any) {
  ;(api.createProject as any).mockResolvedValue(res)
  await act(async () => { byText(el, '新建').click() })
  const inputs = el.querySelectorAll('input')
  await act(async () => {
    typeInto(inputs[0] as HTMLInputElement, '探针项目')
    typeInto(inputs[1] as HTMLInputElement, '改个文案')
  })
  await act(async () => { byText(el, '创建').click() })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

describe('项目页 · 小活走轻量的提示条', () => {
  it('后端建议轻量 → 提示条出现，且带上了服务端给的理由', async () => {
    const el = await renderProjects()
    await createThroughUI(el, {
      ok: true, project: { id: 'p1', name: '探针项目' },
      suggested_flow: { weight: 'light', reason: '没提到调研/架构/方案，而且描述很短' },
    })

    expect(el.textContent, '提示条没渲染 —— 「小活走轻量」又变成死功能了').toContain('走轻量？')
    expect(el.textContent, '理由没透出来（那句话是服务端给的，别在前端重编）')
      .toContain('没提到调研/架构/方案')
    expect(el.textContent).toContain('探针项目')
  })

  it('点「采纳」→ 真的把 light 落到那个项目上，并收起提示条', async () => {
    const el = await renderProjects()
    await createThroughUI(el, {
      ok: true, project: { id: 'p1', name: '探针项目' },
      suggested_flow: { weight: 'light', reason: '描述很短' },
    })
    ;(api.setFlowWeight as any).mockResolvedValue({ ok: true })

    await act(async () => { byText(el, '采纳').click() })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })

    expect(api.setFlowWeight, '采纳没调接口 —— 点了等于没点').toHaveBeenCalledWith('p1', 'light')
    expect(el.textContent, '采纳之后提示条该收起来').not.toContain('走轻量？')
  })

  it('后端没给建议（老后端 / 大活）→ 不出提示条，而且创建流程要照常走完', async () => {
    const el = await renderProjects()
    const before = (api.projects as any).mock.calls.length
    await createThroughUI(el, { ok: true, project: { id: 'p2', name: '正经项目' } })
    expect(el.textContent, '服务端没说，前端自己编了一条').not.toContain('走轻量？')
    // ⚠️ 这条断言不是凑数：`create()` 末尾那句 `fetch()` 在提示块**之后**，
    // 所以"没刷新"正是"提示块里抛了"的可观测后果。
    // 不加这条，把守卫去掉（`res.suggested_flow.reason` 对 undefined 取值）时
    // **用例仍然全绿**，红只出现在汇总行的 `Errors 1 error`（未处理的 rejection）
    // —— 那正是"绿得不稳"那类，不算判据。2026-09-15 变异实测。
    expect((api.projects as any).mock.calls.length,
           'create() 没走到结尾的刷新 —— 提示那块中途抛了').toBeGreaterThan(before)
  })
})
