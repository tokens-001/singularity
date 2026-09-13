// @vitest-environment jsdom
/**
 * 技能页 —— **读不到绑定 ≠ 没有绑定**。
 *
 * ⚠️ 原来 `agentSkills` 拉失败时静默落 `[]`，界面上"读不到"和"一个都没绑"长得一模一样；
 * 用户这时勾任意一个，`updateAgentSkills` 会把后端**原有绑定整体覆盖成勾的那一项**
 * （2026-09-14，外派④扫前端抓出、我核过）。同页还有一条**加载路径上的写副作用**：
 * 单 agent 且绑定为空就自动 PUT 全部技能 —— 它紧跟在"失败落空数组"之后，
 * 于是**一次网络抖动就能把全部技能写上去**。
 *
 * 锁三件事：① 读不到时不说"0/N" ② 读不到时**点不动、也不发请求** ③ 正常路径照常能改。
 * harness 照抄 AgentsTab.test.tsx（裸 createRoot + act + antd App 包裹）。
 */
import { describe, it, expect, beforeAll, vi } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { App as AntApp } from 'antd'

const SKILLS = [
  { name: 'code-review', description: '审查', type: 'prompt' },
  { name: 'ddd', description: 'DDD', type: 'prompt' },
]
const AGENTS = { any: [{ model: 'm1', max_turns: 5 }] }

/** 哪些模型的绑定"读不到"（模拟网络失败） */
let unreadable = new Set<string>()
const writes: any[] = []

vi.mock('../lib/api', () => ({
  api: {
    skills: () => Promise.resolve(SKILLS),
    agents: () => Promise.resolve(AGENTS),
    agentSkills: (m: string) => unreadable.has(m)
      ? Promise.reject(new Error('boom'))
      : Promise.resolve({ skills: [] }),
    phaseSkills: () => Promise.resolve({ skills: [] }),
    updateAgentSkills: (m: string, s: string[]) => { writes.push([m, s]); return Promise.resolve({ ok: true }) },
    updatePhaseSkills: () => Promise.resolve({ ok: true }),
    addSkill: () => Promise.resolve({ ok: true }),
  },
}))

import SkillsTab from './SkillsTab'

beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
  g.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})

async function mount(): Promise<HTMLDivElement> {
  const el = document.createElement('div')
  document.body.appendChild(el)
  await act(async () => { createRoot(el).render(<AntApp><SkillsTab /></AntApp> as ReactNode) })
  return el
}

async function click(el: HTMLElement) {
  // ⚠️ 用 `el.click()` 时**不会触发** antd `Tag.CheckableTag` 的 onChange ——
  // 实测"不发请求"那条会**碰巧过**（假绿）。必须派一个冒泡的真事件。
  await act(async () => {
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
  })
}

describe('读不到技能绑定时的行为', () => {
  it('读不到就说"读不到"，不许装成 0/N', async () => {
    unreadable = new Set(['m1'])
    writes.length = 0
    const el = await mount()

    // 断言**贴着模型名**看：阶段那几行读到的是真的空表，本来就该显示 0/2，
    // 所以不能对整个页面文本下"不含 0/2"这种宽断言（第一版就是这么写错的）。
    expect(el.textContent).toContain('读不到绑定')
    expect(el.textContent, '模型那一行还在装成 0/2').toContain('M1读不到绑定')
  })

  it('读不到时「全选」**点不动、也不发请求**', async () => {
    // ⚠️ 用「全选」而不是点技能标签来测：jsdom 里 `Tag.CheckableTag` 的 onChange
    // 用派发事件也触发不了（实测），拿它当判据会得到一条**碰巧过的假绿**。
    // 「全选」是个真 `<button>`，点了就一定走 onClick。
    unreadable = new Set(['m1'])
    writes.length = 0
    const el = await mount()

    const btn = [...el.querySelectorAll('button')].find(b => (b.textContent || '').includes('全选'))
    expect(btn, '找不到全选框').toBeTruthy()
    expect((btn as HTMLButtonElement).disabled, '读不到时全选该是禁用的').toBe(true)
    await click(btn as HTMLElement)

    expect(writes, `读不到却写了：${JSON.stringify(writes)}`).toEqual([])
  })

  it('**加载时不许偷偷写**（原来单 agent + 空绑定会自动 PUT 全部技能）', async () => {
    // 这一条钉的是"加载路径上的写副作用"：用户把自己清空的绑定一刷新就全回来了
    unreadable = new Set()
    writes.length = 0
    await mount()

    expect(writes, `加载就写了：${JSON.stringify(writes)}`).toEqual([])
  })

  it('对照：读得到时「全选」照常能写', async () => {
    unreadable = new Set()
    writes.length = 0
    const el = await mount()

    const btn = [...el.querySelectorAll('button')].find(b => (b.textContent || '').includes('全选'))
    await click(btn as HTMLElement)

    expect(writes).toEqual([['m1', ['code-review', 'ddd']]])
  })

  it('对照：读得到时全选按钮**不是**禁用的', async () => {
    unreadable = new Set()
    const el = await mount()
    const btn = [...el.querySelectorAll('button')].find(b => (b.textContent || '').includes('全选'))
    expect((btn as HTMLButtonElement).disabled).toBe(false)
  })
})
