// @vitest-environment jsdom
/**
 * 侧边栏导航**不许把"正等人审批的项目"丢掉**（2026-09-15 真机撞出来的，用户原话"审核消失了"）。
 *
 * 病因：点「对话」原来**无条件** `setActiveProject('_default')`，而
 * `Chat.tsx` 的审批面板是 `{isGate && info && <GatePanel/>}`、`info` 又依赖 activeProjectId
 * ⇒ 面板整块不渲染。后端一直好好的，**是前端把"当前项目"丢了**。
 *
 * 这条**没有别的测试守着**（`GatePanel.test.tsx` 测的是面板自己的渲染，
 * 没有任何一条覆盖"导航会不会把 activeProjectId 清掉"）—— 纯靠真机点出来的。
 *
 * harness 照抄 `pages/Alerts.test.tsx`（裸 createRoot + act）。
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest'
import { act } from 'react'
import { createRoot } from 'react-dom/client'

// ⚠️ **必须用 `vi.hoisted`**：zustand 的 persist 中间件在**模块导入那一刻**就去取
// `localStorage`（`persist(...)` 求值时 rehydrate），放进 `beforeAll` 已经太晚 ——
// 那时 store 早就把 storage 解析成 undefined 了（症状：setState 报
// `Cannot read properties of undefined (reading 'setItem')`）。
// 这个 jsdom 环境本来就没有 localStorage（跑别的测试时会打 "localStorage is not available"）。
vi.hoisted(() => {
  const mem = new Map<string, string>()
  ;(globalThis as any).localStorage = {
    getItem: (k: string) => (mem.has(k) ? mem.get(k)! : null),
    setItem: (k: string, v: string) => { mem.set(k, String(v)) },
    removeItem: (k: string) => { mem.delete(k) },
    clear: () => mem.clear(),
    key: (i: number) => Array.from(mem.keys())[i] ?? null,
    get length() { return mem.size },
  }
})

vi.mock('../lib/api', () => ({
  api: {
    projects: vi.fn(), loopStatus: vi.fn(), conflicts: vi.fn(), approvals: vi.fn(),
  },
}))
vi.mock('../lib/useSSE', () => ({ useSSE: () => {}, useSSEConnected: () => true }))
vi.mock('../lib/toast', () => ({ useToast: () => () => {}, useModal: () => ({}) }))
vi.mock('../lib/pinned', () => ({ getPinned: () => [] }))
const navigateSpy = vi.fn()
vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateSpy,
  useLocation: () => ({ pathname: '/' }),
  Outlet: () => null,
}))

import { api } from '../lib/api'
import { useAppStore } from '../stores/app'
import AppLayout from './AppLayout'

beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
  g.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => {
  vi.clearAllMocks()
  ;(api.loopStatus as any).mockResolvedValue({ running: false })
  ;(api.conflicts as any).mockResolvedValue({ conflicts: [] })
  ;(api.approvals as any).mockResolvedValue({ approvals: [] })
})

async function renderLayout(projects: any[], activeId: string) {
  ;(api.projects as any).mockResolvedValue(projects)
  useAppStore.setState({ activeProjectId: activeId })
  const el = document.createElement('div')
  document.body.appendChild(el)
  await act(async () => { createRoot(el).render(<AppLayout />) })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  return el
}

const clickNav = async (el: HTMLElement, label: string) => {
  const btn = Array.from(el.querySelectorAll('button'))
    .find(b => (b.textContent || '').trim() === label)
  expect(btn, `导航里找不到「${label}」`).toBeTruthy()
  await act(async () => { (btn as HTMLButtonElement).click() })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

const GATED = { id: 'p1', name: '测试01', phase: 'gate3' }

describe('侧边栏导航 vs 待审批的项目', () => {
  it('项目停在 GATE 时点「对话」→ 不许把它清掉', async () => {
    const el = await renderLayout([GATED], 'p1')
    await clickNav(el, '对话')

    expect(useAppStore.getState().activeProjectId,
           '待审批的项目被导航清掉了 —— 审批面板会凭空消失（Chat 的 info 变 null）').toBe('p1')
    expect(navigateSpy).toHaveBeenCalledWith('/')
  })

  it('三个 GATE 都算（gate1 / gate2 / gate3）', async () => {
    for (const phase of ['gate1', 'gate2', 'gate3']) {
      const el = await renderLayout([{ ...GATED, phase }], 'p1')
      await clickNav(el, '对话')
      expect(useAppStore.getState().activeProjectId, `${phase} 被清掉了`).toBe('p1')
      el.remove()
    }
  })

  it('项目**不在** GATE 时，行为跟原来一样（回到通用对话）', async () => {
    const el = await renderLayout([{ id: 'p1', name: '跑着的', phase: 'executing' }], 'p1')
    await clickNav(el, '对话')
    expect(useAppStore.getState().activeProjectId,
           '没在等审批，就该照旧切回无项目的通用对话').toBe('_default')
  })

  it('点别的导航（项目/任务…）本来就不该动 activeProjectId', async () => {
    const el = await renderLayout([GATED], 'p1')
    await clickNav(el, '任务')
    expect(useAppStore.getState().activeProjectId).toBe('p1')
  })
})
