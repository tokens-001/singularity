// @vitest-environment jsdom
/**
 * GATE3 验收摘要渲染测试 —— 锁住"核验不了绝不显示成通过"。
 *
 * 后端在不带产出参数时会如实返回 reason="无法核验 —— 这不是通过"（但 passed=true）。
 * 前端要是只读 passed 渲染个绿色"通过"，就把谎撒出去了 —— 和用量页那个
 * "$0.00 看着可信"是同一类 bug。harness 照抄 lib/usage.test.tsx（裸 createRoot + act）。
 */
import { describe, it, expect } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { AcceptancePanel, ProjectArchive, GatePanel, gateCopy } from './GatePanel'

function render(node: ReactNode): string {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const root = createRoot(el)
  act(() => { root.render(node) })
  const text = el.textContent || ''
  act(() => { root.unmount() })
  el.remove()
  return text
}

/** 取摘要行里那个 `QA：…` 的**颜色** —— `render()` 只回 textContent，颜色断言得走 DOM。 */
function qaSpanColor(acceptance: any): string {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const root = createRoot(el)
  act(() => { root.render(<AcceptancePanel acceptance={acceptance} />) })
  const span = Array.from(el.querySelectorAll('span'))
    .find((s) => (s.textContent || '').startsWith('QA：')) as HTMLElement | undefined
  const color = span?.style.color || ''
  act(() => { root.unmount() })
  el.remove()
  return color
}

describe('GATE3 验收摘要', () => {
  it('QA 有问题时显示问题数，不是"通过"', () => {
    const t = render(<AcceptancePanel acceptance={{
      qa_report: { issues: [{ severity: 'critical', description: '示例' }],
                   summary: { failed: 1, verdict: 'no_go' } },
    }} />)
    expect(t).toContain('1 个问题')
    expect(t).not.toContain('QA：通过')
  })

  it('QA 干净才显示通过', () => {
    const t = render(<AcceptancePanel acceptance={{
      qa_report: { issues: [], summary: { failed: 0, verdict: 'go' } },
    }} />)
    expect(t).toContain('QA：通过')
  })

  it('符合性无法核验时绝不显示"通过"', () => {
    const t = render(<AcceptancePanel acceptance={{
      qa_report: null,
      conformance: { passed: true, reason: '无法核验 —— 这不是通过',
                     evidence: { unverifiable: true } },
    }} />)
    expect(t).toContain('无法核验')
    expect(t).not.toContain('需求符合性：通过')
  })

  it('没有 QA 报告时如实说没有，不谎报通过', () => {
    const t = render(<AcceptancePanel acceptance={{}} />)
    expect(t).toContain('QA：无报告')
    expect(t).not.toContain('QA：通过')
  })

  it('QA 报告读不出来时显示"核不了"，不是"通过"', () => {
    // 🔴 这是 `a33bf79` 引入的真洞（2026-09-14 外派反审抓到）：
    // 后端读不出 qa_report.json 时给的占位符 `{"error": …}` 是**真值**，
    // 于是 `!!qa` 成立、`sum` 落成 {}、`failed` 算成 0 ⇒ 渲染成绿色「通过」。
    // **删掉 `qaUnverifiable` 那一行它会红。**
    const t = render(<AcceptancePanel acceptance={{
      qa_report: { error: '报告读不出来（JSONDecodeError）—— 不代表没跑过 QA，先修好文件再看结论' },
    }} />)
    expect(t).not.toContain('QA：通过')
    expect(t).toContain('QA：核不了')
    // 后端那句解释得**露在人审门上** —— 否则它只活在 alerts.jsonl 里，坐在这道门前的人看不到
    expect(t).toContain('不代表没跑过 QA')
  })

  it('报告是个真值但没有 summary ⇒ 一律当核不了', () => {
    // 判据钉的是"**像不像一份报告**"，不是"是不是真值" ——
    // 只判真值的话，换个形状的占位符又会重新爬成绿灯。
    const t = render(<AcceptancePanel acceptance={{ qa_report: { foo: 1 } }} />)
    expect(t).not.toContain('QA：通过')
    expect(t).toContain('QA：核不了')
  })

  it('"核不了"要用琥珀色，不是绿色 —— 与隔壁 conformance 同口径', () => {
    // ⚠️ 光断言文字不够：**变异验证时"只改颜色"那一刀没被抓住**
    // （文字仍是"核不了"，但颜色掉回绿色系就骗人了）。颜色是这条修复的理由本身
    // —— 隔壁 `需求符合性` 的"无法核验"早就是琥珀色，同一个"我核不了"不该一边琥珀一边绿。
    expect(qaSpanColor({ qa_report: { error: 'x' } })).toBe('rgb(180, 83, 9)')   // #b45309 琥珀
    expect(qaSpanColor({ qa_report: { issues: [], summary: { failed: 0, verdict: 'go' } } }))
      .toBe('rgb(22, 163, 74)')                                                  // #16a34a 绿（对照）
  })

  it('项目 issues 要露在折叠框外 —— 验收被跳过不能是静默的', () => {
    const t = render(<AcceptancePanel acceptance={{}} projectIssues={[
      { type: 'verification_skipped', detail: '验收跳过: 架构没产出约束清单, QA/安全审计师都没跑' },
    ]} />)
    expect(t).toContain('验收跳过')
    expect(t).toContain('架构没产出约束清单')
  })
})


// ═══════════════════════════════════════════════════════════════
// 项目档案：不在闸门时也能回看架构/调研（2026-09-15 用户提的）
// ═══════════════════════════════════════════════════════════════
// 原来对话页只在 `isGate` 时挂 `GatePanel`，而架构只在 GATE2/GATE3 露脸
// ⇒ **批完就再也看不见自己批过什么了**，想回看只能去项目页。
// ⚠️ 修法**不是**让 GatePanel 常驻（它是审批条：`🛑 GATE{n}` + 通过/打回，
// 按钮无条件渲染）—— 而是把材料（`ArchitectureDetails` / `ResearchReport`）
// 单独挂一份。这条钉的就是"**挂的是材料，不是审批条**"。

const ARCH = {
  architecture: '主设计：单文件 CLI',
  modules: [{ name: 'cli', responsibility: '参数解析' }],
  tasks: [{ id: 'T1', title: '实现主逻辑' }],
}
const RESEARCH = { pitfalls: ['编码坑'], competitive_analysis: { products: [] } }

function dom(node: ReactNode): HTMLElement {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const root = createRoot(el)
  act(() => { root.render(node) })
  return el
}

describe('项目档案（不在闸门时回看架构/调研）', () => {
  it('架构和调研都在时，两份材料都挂上', () => {
    const el = dom(<ProjectArchive info={{ architecture: ARCH, research_report: RESEARCH }} />)
    const text = el.textContent || ''
    expect(text).toContain('架构方案')
    expect(text).toContain('调研报告')
    el.remove()
  })

  it('**不许出现审批条**：没有 GATE 横幅，也没有通过/打回按钮', () => {
    const el = dom(<ProjectArchive info={{ architecture: ARCH, research_report: RESEARCH }} />)
    const text = el.textContent || ''
    expect(text, '项目档案里冒出了 GATE 横幅 —— 挂错组件了').not.toContain('GATE')
    const labels = Array.from(el.querySelectorAll('button')).map(b => (b.textContent || '').trim())
    expect(labels.some(l => l.includes('通过')), '不该有「通过」按钮').toBe(false)
    expect(labels.some(l => l.includes('打回')), '不该有「打回」按钮').toBe(false)
    el.remove()
  })

  it('**默认是收起的** —— 别把对话页撑爆', () => {
    const el = dom(<ProjectArchive info={{ architecture: ARCH }} />)
    // 材料都是 <details>，未展开时 detail 内容不该可见
    const details = el.querySelectorAll('details')
    expect(details.length, '材料该是 <details>（默认收起）').toBeGreaterThan(0)
    expect(Array.from(details).every(d => !d.hasAttribute('open')), '默认就展开了').toBe(true)
    el.remove()
  })

  it('没有材料时 → 整个不渲染（别在对话页留个空壳）', () => {
    // 契约是"缺省 = falsy"：后端没有架构时给的是 **`null`**（实测 `/api/projects` 里
    // `architecture: None`），不是 `{}`。
    expect((dom(<ProjectArchive info={{}} />).textContent || '').trim()).toBe('')
    expect((dom(<ProjectArchive info={null} />).textContent || '').trim()).toBe('')
    expect((dom(<ProjectArchive info={undefined} />).textContent || '').trim()).toBe('')
    document.querySelectorAll('div').forEach(d => { if (!d.textContent?.trim()) d.remove() })
  })

  it('只有调研（架构还没生成）→ 只挂调研，不炸', () => {
    const el = dom(<ProjectArchive info={{ research_report: RESEARCH }} />)
    const text = el.textContent || ''
    expect(text).toContain('调研报告')
    expect(text).not.toContain('架构方案')
    el.remove()
  })
})

/**
 * GATE2 **兼任两个完全不同的角色**，长文案却按门号写死 ⇒ 两种处境长得一模一样。
 *
 * 真机 2026-09-15：用户在 GATE2 上当场问「**为什么任务都执行失败了，还有架构审核**」——
 * 那条横幅底下其实是 `integrating` 里「审查自动修已达上限(2轮), 升 GATE2 人工兜底」，
 * 而**理由一直躺在盘上**（`lineage` 里那条 `reason`），界面不读它。
 */
describe('GATE2 兜底升上来时要说真话', () => {
  const 兜底项目 = {
    owner_confirm: { gate1: 'approved', gate2: 'approved' },
    lineage: [
      { action: 'phase', from: 'planning', to: 'gate2', reason: '架构完成' },
      { action: 'phase', from: 'gate2', to: 'executing', reason: '人工批准' },
      { action: 'phase', from: 'integrating', to: 'gate2',
        reason: '审查自动修已达上限(2轮), 升GATE2人工兜底' },
    ],
  }

  it('初次审架构 ⇒ 还是原来那句', () => {
    const c = gateCopy('2', { owner_confirm: {}, lineage: [] })
    expect(c.label).toBe('架构完成·请审核方案')
    expect(c.reason).toBe('')
  })

  it('本门批过又回来 ⇒ 改口，并把来路摆出来', () => {
    const c = gateCopy('2', 兜底项目)
    expect(c.label).toContain('兜底')
    expect(c.label).not.toContain('架构完成·请审核方案')   // ← 最要命的那条：别再显示成初次审核
    expect(c.reason).toContain('审查自动修已达上限')
  })

  it('GATE1/GATE3 不受影响（别把修法改宽）', () => {
    expect(gateCopy('1', 兜底项目).label).toBe('定义完成·请审核PRD')
    expect(gateCopy('3', 兜底项目).label).toBe('验收完成·请审核交付物')
  })

  it('字段缺失/不是数组也不许炸', () => {
    expect(() => gateCopy('2', undefined)).not.toThrow()
    expect(() => gateCopy('2', { owner_confirm: { gate2: 'approved' }, lineage: '不是数组' }))
      .not.toThrow()
    expect(gateCopy('2', { owner_confirm: { gate2: 'approved' }, lineage: '不是数组' }).reason).toBe('')
  })

  it('面板上真的渲染出来了（接线，不只是函数对）', () => {
    const text = render(<GatePanel info={兜底项目} gateNum="2" gatePhase="gate2"
                                   onGate={() => {}} />)
    expect(text).toContain('审查自动修已达上限')
    expect(text).not.toContain('架构完成·请审核方案')
  })
})
