// @vitest-environment jsdom
/**
 * 对话页顶部那行**项目用量**的接线测试（2026-09-17）。
 *
 * 用户原话：「看不到单独项目 token 用量，我建议放在独立项目对话框」。
 * 数据其实一直在（`/api/token-usage` 的 `by_project`），**只是前端一处都没读过**
 * （`project_cost` 接口也一样，grep 零命中）—— 典型的"接口有、界面没接线"。
 *
 * 这条守的就是那根线：**删掉 `api.tokenUsage()` 那次调用、或者不渲染 `usage`，
 * 它会红**。只测 `fmtTokens` 是没用的 —— 那正是"函数对≠接线通"。
 *
 * ⚠️ 顺带钉住一件容易骗人的事：后端 `per_project_usage` **只汇总今天**
 *    ⇒ 那一行必须写明「今日」。不然昨天跑完的项目今天显示 0，看着像坏了。
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { App as AntApp, ConfigProvider } from 'antd'
import { antdTheme } from '../lib/theme'

vi.hoisted(() => {
  const m: Record<string, string> = {}
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (k: string) => (k in m ? m[k] : null),
      setItem: (k: string, v: string) => { m[k] = String(v) },
      removeItem: (k: string) => { delete m[k] }, clear: () => {},
    },
  })
})

const PROJECT = { id: 'p1', name: '日志统计工具', phase: 'executing' }

vi.mock('../lib/api', () => ({
  api: {
    status: vi.fn(), projects: vi.fn(), tasks: vi.fn(), tokenUsage: vi.fn(),
    traceability: vi.fn(), taskTrace: vi.fn(), observerChat: vi.fn(),
    createProject: vi.fn(), gateConfirm: vi.fn(), retryTask: vi.fn(),
    revealFile: vi.fn(), fsPick: vi.fn(), setProjectsRoot: vi.fn(),
  },
  rejectErrorPlaceholder: (d: any) => d,
}))
// SSE 回调存下来 —— 测"切项目要重取"得能手动喂一个 project 事件进去
// （`fetchProjects` 是被 SSE 的 project 事件驱动的，不喂事件页面不会去重取）。
const sseHandlers: ((e: any) => void)[] = []
vi.mock('../lib/useSSE', () => ({
  useSSE: (fn: (e: any) => void) => { sseHandlers.push(fn) },
  useSSEConnected: () => true,
}))

import { api } from '../lib/api'
import { useAppStore } from '../stores/app'
import Chat from './Chat'

beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
  g.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  g.matchMedia = g.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
})

beforeEach(() => {
  vi.clearAllMocks()
  sseHandlers.length = 0
  useAppStore.setState({ conversations: {}, activeProjectId: 'p1' })
  ;(api.status as any).mockResolvedValue({ counts: {}, alerts: [], alert_summary: [] })
  ;(api.projects as any).mockResolvedValue({ projects: [PROJECT] })
  ;(api.tasks as any).mockResolvedValue([])
  // ⚠️ 故意带一行**别的项目**（而且排在前面）—— 少了这个诱饵，
  // "取匹配的那行"和"取第一行"结果一样，那条变异就掐不断（本仓栽过这个形状）。
  ;(api.tokenUsage as any).mockResolvedValue({
    by_project: [
      { project_id: 'p_别的', project_name: '别的项目', tokens: 999999, cost: 9.99, tasks: 99 },
      { project_id: 'p1', project_name: '日志统计工具', tokens: 12345, cost: 0.0123, tasks: 7 },
    ],
    unpriced_models: [],
  })
})

async function mount() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const root = createRoot(el)
  const wrap = (n: ReactNode) => <ConfigProvider theme={antdTheme}><AntApp>{n}</AntApp></ConfigProvider>
  await act(async () => { root.render(wrap(<Chat />)) })
  await act(async () => { await Promise.resolve() })
  return { el, done: () => { act(() => root.unmount()); el.remove() } }
}

const TASK = (id: string, desc: string, status = 'running') => ({
  id, description: desc, status, project_id: 'p1', updated_at: 1, created_at: 1,
})

const clickText = async (el: HTMLElement, label: string) => {
  const btn = Array.from(el.querySelectorAll('button'))
    .find(b => (b.textContent || '').trim() === label)
  expect(btn, `没找到「${label}」按钮`).toBeTruthy()
  await act(async () => { (btn as HTMLElement).click() })
}

/**
 * 布局：**状态钉住、材料侧滑、正文留给观察者**（2026-09-17 用户提：
 * 「之前忽略了观察者对话窗口，导致现在调研架构等任务都堆在对话窗口」）。
 *
 * 根子是**两种性质不同的东西共用了一条时间轴**：
 *   · 对话 = 流水（只增不减，要能往回翻）
 *   · 项目状态 = 快照（永远只有当前这一份，要能**一眼看到**）
 * 塞在一起必然打架。⇒ 状态钉顶栏、材料收进侧滑面板。
 *
 * 🔴 **门禁那根条必须在顶栏**：它是「状态 + 一个动作」。放进材料面板的话，
 *    用户不点开就看不见"该我审批了" —— 正是这个仓反复栽的 #28 那一族。
 */
describe('常驻状态条 + 材料侧滑面板', () => {
  it('顶栏常驻：项目名 / 阶段 / 进度（带总数）', async () => {
    ;(api.tasks as any).mockResolvedValue([TASK('t1', 'a', 'done'), TASK('t2', 'b')])
    const { el, done } = await mount()
    const text = (el.textContent || '').replace(/\s+/g, ' ')
    expect(text).toContain('日志统计工具')
    expect(text).toContain('实现中')
    expect(text, '进度没带总数 —— 看不出还剩几个').toContain('1/2')
    done()
  })

  it('任务卡**默认不在正文里**（要点「📁 材料」才出来）', async () => {
    ;(api.tasks as any).mockResolvedValue([TASK('t1', '实现解析器')])
    const { el, done } = await mount()
    expect(el.textContent, '任务卡又堆回正文了').not.toContain('实现解析器')
    done()
  })

  it('点「📁 材料」→ 材料出来；再点 ✕ → 收回去', async () => {
    ;(api.tasks as any).mockResolvedValue([TASK('t1', '实现解析器')])
    const { el, done } = await mount()
    await clickText(el, '📁 材料')
    expect(el.textContent, '点了材料却没出来').toContain('实现解析器')
    await act(async () => {
      ;(el.querySelector('[aria-label="关闭材料面板"]') as HTMLElement).click()
    })
    expect(el.textContent, '关不掉').not.toContain('实现解析器')
    done()
  })

  it('**门禁按钮在顶栏、不点材料也看得见** —— 收进去就可能看不见该审批', async () => {
    ;(api.projects as any).mockResolvedValue({ projects: [{ ...PROJECT, phase: 'gate1' }] })
    ;(api.tasks as any).mockResolvedValue([TASK('t1', 'x')])
    const { el, done } = await mount()
    // 没点「📁 材料」
    expect(el.textContent, '门禁按钮没渲染出来').toMatch(/通过/)
    expect(el.textContent).toMatch(/打回/)
    // 而且正文（材料面板之外）里也确实有 —— 不是被藏在抽屉里
    expect(el.textContent, '任务卡不该同时露在外面').not.toContain('x')
    done()
  })
})

describe('对话页顶部的项目用量', () => {
  it('把**这个项目**的 token 显示出来（而不是全站总量）', async () => {
    const { el, done } = await mount()
    const text = (el.textContent || '').replace(/\s+/g, ' ')
    expect(text, '没调用项目用量接口').toContain('12.3k')
    expect(text).toContain('tokens')
    done()
  })

  it('要写明「今日」—— 后端只汇总今天，不写会骗人', async () => {
    const { el, done } = await mount()
    expect(el.textContent || '').toContain('今日')
    done()
  })

  it('没配单价的模型 ⇒ 金额标 `+`（后端明说 cost 只是下限）', async () => {
    ;(api.tokenUsage as any).mockResolvedValue({
      by_project: [{ project_id: 'p1', project_name: 'x', tokens: 1000, cost: 0.5, tasks: 1 }],
      unpriced_models: ['某新模型'],
    })
    const { el, done } = await mount()
    const text = (el.textContent || '').replace(/\s+/g, ' ')
    expect(text, 'cost 是下限却没标 +').toContain('0.5000+')
    done()
  })

  it('切到别的项目 ⇒ 显示那个项目的用量，不是上一个的', async () => {
    const { el, done } = await mount()
    expect(el.textContent || '').toContain('12.3k')

    ;(api.projects as any).mockResolvedValue({ projects: [PROJECT, { id: 'p2', name: '另一个', phase: 'done' }] })
    ;(api.tokenUsage as any).mockResolvedValue({
      by_project: [
        { project_id: 'p1', project_name: '日志统计工具', tokens: 12345, cost: 0.0123, tasks: 7 },
        { project_id: 'p2', project_name: '另一个', tokens: 777, cost: 0.001, tasks: 1 },
      ],
      unpriced_models: [],
    })
    useAppStore.setState({ activeProjectId: 'p2' })
    // 喂一个带 project_id 的事件 → 页面重取项目列表（这次含 p2）→ info/gatePhase 变 → 重取用量。
    // ⚠️ 用**最后注册**的那个 handler：每次 render 都会重新 `useSSE(fn)`，
    // 旧闭包里的 `activePid` 还是上一个项目。
    await act(async () => { sseHandlers[sseHandlers.length - 1]({ kind: 'system', project_id: 'p2', msg: '' }) })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    const text = (el.textContent || '').replace(/\s+/g, ' ')
    expect(text, '切项目后还显示上一个项目的用量').toContain('777')
    done()
  })
})
