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
/** 哪些**阶段**的默认绑定"读不到" */
let phaseUnreadable = new Set<string>()
/** 列表级那两下（`api.skills` / `api.agents`）读得到吗 —— 抛的话整页该怎么显示 */
let skillsUnreadable = false
const writes: any[] = []

vi.mock('../lib/api', () => ({
  api: {
    skills: () => skillsUnreadable ? Promise.reject(new Error('boom')) : Promise.resolve(SKILLS),
    agents: () => Promise.resolve(AGENTS),
    agentSkills: (m: string) => unreadable.has(m)
      ? Promise.reject(new Error('boom'))
      : Promise.resolve({ skills: [] }),
    phaseSkills: (p: string) => phaseUnreadable.has(p)
      ? Promise.reject(new Error('boom'))
      : Promise.resolve({ skills: [] }),
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

  // ── 阶段那条轴的派生值（2026-09-14 外派⑦ 抓到）────────────────────────
  // 「某个阶段读不到」原来被 `(x?.length ?? 0)` 碾成 0 ⇒ 模型行**该出的警示不出**：
  // 用户看着"这份绑定没被覆盖"，跑起来用的是阶段那份 —— 正是这条警示要防的事。
  it('某个阶段读不到时，不许把"判不了"渲染成"没被覆盖"', async () => {
    unreadable = new Set()
    phaseUnreadable = new Set(['planning'])          // 只有"架构"这一档读不到
    const el = await mount()

    expect(el.textContent, '读不到却说"没被覆盖" —— 该出的警示没出')
      .toContain('有的阶段默认读不到')
  })

  it('对照：阶段都读得到、且都为空时，**不**出这条警示', async () => {
    unreadable = new Set()
    phaseUnreadable = new Set()
    const el = await mount()

    expect(el.textContent).not.toContain('有的阶段默认读不到')
  })

  // ── 列表级三态（2026-09-14，外派⑧ 的 C1；见 docs/防御模式.md §70）──────────
  // `api.skills()` 一抛，原来整页停在初值：头部「技能 (0)」是编的，列表区空白。
  // ⚠️ 这不是理论风险 —— 铺开方案第 9 步正打算给 `api.skills` 加"缺键就抛"。
  it('技能列表读不到时，头部不许显示 (0)', async () => {
    unreadable = new Set(); phaseUnreadable = new Set(); skillsUnreadable = true
    const el = await mount()

    expect(el.textContent, '读不到却显示「技能 (0)」—— 那是在编数字').not.toContain('技能 (0)')
    expect(el.textContent).toContain('技能 （读不到）')
    expect(el.textContent).toContain('读不到技能 / 智能体列表')
    expect(el.textContent).toContain('不知道')
  })

  it('这条错误态**有出口**：点「重试」能恢复成真数据', async () => {
    // ⚠️ 第一版这条写的是"读不到时不许出现「全选」" —— **那是假绿**：
    // 没有修复时整个 fetch 抛掉、什么都不渲染，照样没有「全选」，两边都过。
    // 换成能分辨的：错误块必须**真能点回去**（顺带钉住 wiring）。
    unreadable = new Set(); phaseUnreadable = new Set(); skillsUnreadable = true
    const el = await mount()
    expect(el.textContent).toContain('读不到技能 / 智能体列表')

    skillsUnreadable = false                       // 后端好了
    const btn = [...el.querySelectorAll('button')].find(b => (b.textContent || '').includes('重试'))
    expect(btn, '错误态没有重试按钮 = 把人堵死在失败里').toBeTruthy()
    await click(btn as HTMLElement)

    expect(el.textContent).toContain('技能 (2)')
    expect(el.textContent).not.toContain('读不到技能 / 智能体列表')
  })

  it('对照：列表读得到时头部是**真数**、也没有那条错误块', async () => {
    unreadable = new Set(); phaseUnreadable = new Set(); skillsUnreadable = false
    const el = await mount()

    expect(el.textContent).toContain('技能 (2)')          // SKILLS 有两条
    expect(el.textContent).not.toContain('读不到技能 / 智能体列表')
  })
})
