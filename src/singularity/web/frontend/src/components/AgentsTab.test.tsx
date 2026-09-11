// @vitest-environment jsdom
/**
 * 智能体页 —— 「激活池」和「阶段 → 模型」是两件事。
 *
 * 锁住两处：
 * ① 禁用区不显示模型库里已经删掉的名字（幽灵条目）
 * ② 阶段配置里点名的模型，即使已经不在激活池里，也要**列出来标「(已移除)」**，
 *    不能悄悄换成别的 —— 图省事把它过滤掉等于让"我配过"这件事静默消失
 *    （旧的融合页 `.options()` 就是这么干的）。
 *
 * harness 照抄 lib/usage.test.tsx（本项目没有 @testing-library/react，
 * 用裸 createRoot + act，jsdom 缺 ResizeObserver 要补桩）。
 */
import { describe, it, expect, beforeAll, vi } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { App as AntApp } from 'antd'

const AGENTS = {
  any: [{ model: 'live-model', max_turns: 5 }],
  // lib-model 还在模型库里；vanished-model 已经不在 —— 前者该显示，后者该被挡住
  _disabled: { any: ['lib-model', 'vanished-model'] },
}

const MODELS = [
  { id: 'live-model', api_available: true, provider: 'deepseek' },
  { id: 'lib-model', api_available: true, provider: 'kimi' },
]

const PHASE_MODELS = {
  phases: [
    { key: 'planning', label: '架构', hint: '全部 = 委员会席位' },
    { key: 'extract', label: '融合提取', hint: '第 1 个 = 融合提取员' },
  ],
  // gone-model 被点名配过，但现在不在激活池里 —— 必须仍然可见
  custom: { planning: ['live-model', 'gone-model'], extract: ['live-model'] },
}

const updates: any[] = []

/** 让 phaseModels 失败 —— 模拟"后端还没重启，不认识这个新端点"。 */
let phaseModelsFails = false

vi.mock('../lib/api', () => ({
  api: {
    agents: () => Promise.resolve(AGENTS),
    models: () => Promise.resolve(MODELS),
    phaseModels: () => phaseModelsFails
      ? Promise.reject(new SyntaxError('Unexpected token < in JSON'))
      : Promise.resolve(PHASE_MODELS),
    updatePhaseModels: (m: any) => { updates.push(m); return Promise.resolve({ ok: true }) },
    addAgent: () => Promise.resolve({ ok: true }),
    deleteAgent: () => Promise.resolve({ ok: true }),
    updateAgent: () => Promise.resolve({ ok: true }),
  },
}))

import AgentsTab, { buildPhasePayload } from './AgentsTab'

beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
  g.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})

async function render(node: ReactNode) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  await act(async () => { createRoot(el).render(<MemoryRouter><AntApp>{node}</AntApp></MemoryRouter>) })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  return el
}

describe('buildPhasePayload', () => {
  it('去空值、去重且保序 —— 顺序就是"谁是主力"', () => {
    expect(buildPhasePayload({ planning: ['b', 'a', 'b', ''] })).toEqual({ planning: ['b', 'a'] })
  })

  it('空数组删键（清空某项 = 恢复默认，不是存一个空名单）', () => {
    expect(buildPhasePayload({ planning: [], extract: ['x'] })).toEqual({ extract: ['x'] })
  })

  it('全是空值时得到空对象', () => {
    expect(buildPhasePayload({ planning: [], extract: [] })).toEqual({})
  })
})

describe('智能体页', () => {
  it('把「激活池」和「阶段 → 模型」分成两块', async () => {
    const el = await render(<AgentsTab />)
    const text = el.textContent || ''
    expect(text).toContain('已激活的模型')
    expect(text).toContain('阶段 → 模型')
  })

  it('每个阶段一行，用的是后端给的 label', async () => {
    const el = await render(<AgentsTab />)
    const text = el.textContent || ''
    expect(text).toContain('架构')
    expect(text).toContain('融合提取')
    expect(text).toContain('全部 = 委员会席位')
  })

  it('配置里点名但已不在激活池的模型，标「(已移除)」而不是消失', async () => {
    const el = await render(<AgentsTab />)
    expect(el.textContent || '').toContain('(已移除)')
  })

  it('每行标出首选（多选控件本身看不出顺序）', async () => {
    const el = await render(<AgentsTab />)
    expect(el.textContent || '').toContain('首选：')
  })

  it('禁用区只显示模型库里还在的', async () => {
    // 界面上是美化名（mcn），不是原始 id
    const el = await render(<AgentsTab />)
    const text = el.textContent || ''
    expect(text).toContain('Lib Model')
    expect(text).not.toContain('Vanished Model')
  })

  it('激活/移除那套还在（回归）', async () => {
    const el = await render(<AgentsTab />)
    expect(el.textContent || '').toContain('添加')
  })

  it('阶段配置取不到时，激活列表和模型列表照样显示', async () => {
    // 后端没重启（不认识 /api/phase-models）→ 它落到 SPA 兜底返回 HTML → res.json() 抛。
    // 以前这条请求和另外两条一起 Promise.all，一挂全挂：整页"没有模型"。
    phaseModelsFails = true
    try {
      const el = await render(<AgentsTab />)
      const text = el.textContent || ''
      expect(text).toContain('Live Model')     // 激活列表还在
      expect(text).toContain('Lib Model')      // 禁用列表还在
      expect(text).toContain('添加')
    } finally {
      phaseModelsFails = false
    }
  })
})
