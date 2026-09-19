// @vitest-environment jsdom
/**
 * 任务页把「上一轮」的任务和当前批次**分开显示**的接线测试（2026-09-19 补）。
 *
 * 症状：2026-09-17 round d 同一个项目 id 上挂着**两批**任务（旧 9 + 重规划 8 = 17），
 * 界面上平铺，用户当场问「都到十七个了」—— 看着像任务一直在累积。
 * **数据层没问题**（`project.task_ids` 只挂新批），是显示层把两批混在了一起。
 *
 * 判据：**挂在某项目下、却不在它的 `task_ids` 里** = 上一批。
 * 这条数据层本来就有（`_decompose_and_create_tasks` / `_run_execution` 重规划时会
 * `proj.task_ids = []` 再重建），前端只是从来没读它。
 *
 * ⚠️ 第二条用例是**命门**：`task_ids` 拿不到（项目列表还没回来 / 项目已删）时
 * 拿空集去判，会把**整个项目**的任务全标成"上一轮"。没有它，把 `?.size` 那个守卫
 * 删掉也不会有任何测试变红。
 *
 * harness 照抄 `pages/Projects.test.tsx`（裸 createRoot + act，只 mock `api`）。
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { App as AntApp, ConfigProvider } from 'antd'
import { antdTheme } from '../lib/theme'

vi.mock('../lib/api', () => ({
  api: { tasks: vi.fn(), projects: vi.fn(), dagMetrics: vi.fn(), deleteTask: vi.fn() },
}))
vi.mock('../lib/useSSE', () => ({ useSSE: () => {}, useSSEConnected: () => true }))
vi.mock('react-router-dom', () => ({
  useSearchParams: () => [new URLSearchParams(), () => {}],
}))

import { api } from '../lib/api'
import Tasks from './Tasks'

beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
  g.ResizeObserver = class { observe() {} unobserve() {} close() {} disconnect() {} }
})
beforeEach(() => { vi.clearAllMocks() })

const task = (id: string, project_id: string, created_at: number) => ({
  id, description: `任务 ${id}`, status: 'done', project_id,
  route_type: '', route_gate: '', route_role: '', updated_at: created_at, created_at,
})

async function renderTasks(tasks: any[], projects: any[]): Promise<HTMLElement> {
  ;(api.tasks as any).mockResolvedValue(tasks)
  ;(api.projects as any).mockResolvedValue(projects)
  ;(api.dagMetrics as any).mockResolvedValue(null)
  const el = document.createElement('div')
  document.body.appendChild(el)
  await act(async () => {
    createRoot(el).render(
      <ConfigProvider theme={antdTheme}><AntApp><Tasks /></AntApp></ConfigProvider> as ReactNode)
  })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  return el
}

describe('任务页 · 上一轮 vs 当前批次', () => {
  it('不在 task_ids 里的任务标「上一轮」，且排在当前批次之后', async () => {
    const el = await renderTasks(
      // 旧批 created_at 更小 —— 不额外排序的话它反而排在前面
      [task('old1', 'p1', 100), task('new1', 'p1', 900)],
      [{ id: 'p1', name: '探针项目', task_ids: ['new1'] }])

    const text = el.textContent || ''
    expect(text, '「上一轮」标签没渲染 —— 两批又平铺在一起了').toContain('上一轮')
    expect(text, '表头没告诉人有几条是上一轮的').toContain('含 1 条上一轮')
    expect(text.indexOf('任务 new1'), '当前批次该排在上一轮前面')
      .toBeLessThan(text.indexOf('任务 old1'))
    el.remove()
  })

  it('当前批次的任务**不该**被标成上一轮', async () => {
    const el = await renderTasks(
      [task('new1', 'p1', 900)],
      [{ id: 'p1', name: '探针项目', task_ids: ['new1'] }])
    expect(el.textContent, '当前批次被误标了').not.toContain('上一轮')
    el.remove()
  })

  it('🔴 task_ids 拿不到 / 是空的时不标 —— 否则整个项目都会变成「上一轮」', async () => {
    // 这条是命门：`isPrevBatch` 里那个 `?.size` 守卫去掉，这条就该红。
    const cases: any[] = [
      { name: '项目列表没带 task_ids', projects: [{ id: 'p1', name: 'p1' }] },
      { name: '项目已删、task_ids 是空数组', projects: [{ id: 'p1', name: 'p1', task_ids: [] }] },
    ]
    for (const c of cases) {
      const el = await renderTasks([task('t1', 'p1', 100)], c.projects)
      expect(el.textContent, `${c.name} 时不该标上一轮`).not.toContain('上一轮')
      el.remove()
    }
  })

  it('独立任务（没有 project_id）永远不标', async () => {
    const el = await renderTasks([task('solo', '', 100)], [])
    expect(el.textContent).not.toContain('上一轮')
    el.remove()
  })
})
