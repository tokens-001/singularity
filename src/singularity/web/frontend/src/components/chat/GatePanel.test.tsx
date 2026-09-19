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
import { AcceptancePanel, ProjectArchive, GatePanel, gateCopy, ResearchReport } from './GatePanel'

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

/**
 * GATE3 **两种"集成通过"长得一样** —— 真跑过测试，和项目里压根没有测试可跑
 * （pytest 退 5）。后端 `orchestrator._note_integration` 记了 `tests_ran` 这道留痕，
 * 这里把它摆到门上；不读它的话「测过了」和「没测」在人眼里一样，
 * 而 GATE3 正是**唯一**决定放不放行的那个点。
 */
describe('GATE3 集成没跑到测试要说真话', () => {
  const 留痕 = (tests_ran: boolean) => ({
    lineage: [{ action: 'integration_merge', ok: true, tests_ran }],
  })

  it('真跑过测试 ⇒ 还是原来那句（别把修法改宽成谁都拦）', () => {
    expect(gateCopy('3', 留痕(true)).label).toBe('验收完成·请审核交付物')
  })

  it('没跑到测试 ⇒ 改口', () => {
    const c = gateCopy('3', 留痕(false))
    expect(c.label).toContain('没跑到测试')
    expect(c.label).not.toContain('验收完成·请审核交付物')
  })

  it('判据是字段不是措辞 —— 只有 tests_ran 才改口', () => {
    // 同一条留痕，措辞换成"没测试"但字段是 true ⇒ 不许改口（文案会变，状态不会）
    const c = gateCopy('3', { lineage: [
      { action: 'integration_merge', ok: true, tests_ran: true, detail: '项目里没有测试可跑' },
    ]})
    expect(c.label).toBe('验收完成·请审核交付物')
  })

  it('老项目没有这条留痕 ⇒ 按原样显示', () => {
    expect(gateCopy('3', { lineage: [{ action: 'phase', to: 'gate3' }] }).label)
      .toBe('验收完成·请审核交付物')
    expect(() => gateCopy('3', { lineage: '不是数组' })).not.toThrow()
  })

  it('面板上真的渲染出来了（接线，不只是函数对）', () => {
    const text = render(<GatePanel info={留痕(false)} gateNum="3" gatePhase="gate3"
                                   onGate={() => {}} />)
    expect(text).toContain('没跑到测试')
    expect(text).not.toContain('验收完成·请审核交付物')
  })
})

/**
 * 调研报告**解析失败时不能是个空框** —— 2026-09-17 真机，用户原话「我怎么不能看报告」。
 *
 * 模型吐的 JSON 坏了（字符串里带裸换行）⇒ 后端 `try_parse_json` 走兜底
 * `{raw_output: 前 5000 字, parse_error: true}` ⇒ 组件里那三个分支（推荐方案 / 竞品 / 关键坑）
 * **一个都不进** ⇒ **渲染成一个空框**（标题还在、点开是空的）。
 * ⚠️ 而这里原来**压根不认识 `parse_error`** ⇒ **一个字都不提示**。
 */
describe('调研报告解析失败要说话，不能是空框', () => {
  const 坏报告 = {
    parse_error: true,
    raw_output: '```json\n{"competitive_analysis": {"products": [{"name": "jq"',
    raw_chars: 20442,
    raw_truncated: true,
  }

  it('要说"解析失败"，而不是什么都不说', () => {
    const text = render(<ResearchReport report={坏报告} projectId="p1" />)
    expect(text).toContain('解析失败')
  })

  it('要把原文摆出来（至少开头），不能是空的', () => {
    const text = render(<ResearchReport report={坏报告} projectId="p1" />)
    expect(text).toContain('competitive_analysis')
  })

  it('被截断就要说清"这是前多少字、全文多少字"', () => {
    const text = render(<ResearchReport report={坏报告} projectId="p1" />)
    expect(text).toContain('20442')
  })

  it('有 projectId 时给一条读全文的链接', () => {
    const el = document.createElement('div')
    document.body.appendChild(el)
    const root = createRoot(el)
    act(() => { root.render(<ResearchReport report={坏报告} projectId="p1" />) })
    const href = el.querySelector('a')?.getAttribute('href') || ''
    act(() => { root.unmount() })
    el.remove()
    expect(href).toBe('/api/projects/p1/research-raw')
  })

  it('正常报告**不受影响**（别把修法改宽）', () => {
    const text = render(<ResearchReport report={{
      recommendation: '用 Python', competitive_analysis: { products: [{ name: 'jq', type: 'x', strengths: 'y' }] },
    }} projectId="p1" />)
    expect(text).toContain('推荐方案')
    expect(text).toContain('jq')
    expect(text).not.toContain('解析失败')
  })
})

// ═══════════════════════════════════════════════════════════════
// 调研报告：每段一行 + 点开看（2026-09-17 用户提的）
// ═══════════════════════════════════════════════════════════════
// 用户原话：「一次显示所有方案太多，我建议改为每个方案的名称摘要，
// 我在选择要不要点开或者下拉查看」。
//
// 改之前是**三块固定内容写死**（推荐方案 / 竞品 / 关键坑），而调研报告实际有 8 段
// —— 另外 5 段（前沿理论 / 用户调研 / 范围澄清 / 技术验证 / 约束）**从来没露过面**，
// 用户根本不知道它们存在。根因是"没写进代码的段永远看不见"，所以这里钉的第一条
// 就是**模型多吐一段必须自动出现**。

const FULL = {
  competitive_analysis: { products: [{ name: 'jq', type: 'CLI', strengths: '生态好' }], comparison: '三档' },
  frontier_theory: { papers: [{ name: 'Drain' }], maturity: '成熟' },
  user_research: { pain_points: ['慢'], needs: ['快'], unmet_needs: '契约' },
  scope_clarification: { core: ['a'], secondary: ['b'], out_of_scope: ['c'], priorities: ['P0'] },
  technical_poc: { pocs: [{ name: 'PoC-1' }], conclusion: '可行' },
  constraints: { performance: ['<150ms'], security: ['零依赖'] },
  recommendation: '纯标准库、分层单遍流式',
  pitfalls: ['A', 'B', 'C'],
}

describe('调研报告：每段一行、点开看', () => {
  it('**8 段全都出现** —— 以前只露 3 段', () => {
    const text = render(<ResearchReport report={FULL} projectId="p1" />)
    for (const label of ['推荐方案', '竞品分析', '关键坑', '前沿理论',
                         '用户调研', '范围澄清', '技术验证', '约束']) {
      expect(text, `「${label}」没渲染出来`).toContain(label)
    }
    expect(text).toContain('8 段')
  })

  it('**模型多吐一段要自动出现** —— 不能"没写进表里就永远看不见"', () => {
    const text = render(<ResearchReport report={{ ...FULL, brand_new: ['x', 'y', 'z'] }}
                                         projectId="p1" />)
    expect(text, '表外的键被吞了 —— 这就是当年只露 3/8 段的同一个形状').toContain('brand_new')
    expect(text).toContain('3 条')
  })

  it('摘要**按值的形状**算，不按键名写死', () => {
    const text = render(<ResearchReport report={{
      recommendation: '很长的字符串'.repeat(20),
      arr: [1, 2, 3, 4, 5],
      obj: { papers: 1, maturity: 2 },
    }} projectId="p1" />)
    expect(text).toContain('5 条')          // 数组 → 条数
    expect(text).toContain('papers')        // 对象 → 点名子块
    expect(text).toContain('maturity')
    expect(text).toContain('…')             // 长字符串 → 截断标记
  })

  it('每段各自一个 `<details>`，且**默认全收起**', () => {
    const el = dom(<ResearchReport report={FULL} projectId="p1" />)
    const details = el.querySelectorAll('details')
    expect(details.length, '8 段该有 8 个折叠条').toBe(8)
    expect(Array.from(details).every(d => !d.hasAttribute('open')), '默认就展开了').toBe(true)
    el.remove()
  })

  it('**每段展开后都有正文** —— 不许出现"点开是空的"', () => {
    // `competitive_analysis` 没有 products 时，旧代码那三个分支一个都不进 ⇒ 空框。
    // 兜底渲染成 JSON 才对：空框和"这段本来就没内容"长得一模一样。
    const el = dom(<ResearchReport report={{
      competitive_analysis: {}, empty_arr: [], nil: null, zero: 0,
    }} projectId="p1" />)
    const blocks = el.querySelectorAll('details')
    expect(blocks.length).toBeGreaterThan(0)
    Array.from(blocks).forEach((d, i) => {
      const body = d.querySelector('div')
      expect((body?.textContent || '').trim(), `第 ${i + 1} 段点开是空的`).not.toBe('')
    })
    el.remove()
  })
})

describe('摘要条不许把锅甩给调研', () => {
  it('解析失败时要说"解析失败"，**不能说成"调研没给推荐方案"**', () => {
    const text = render(<GatePanel info={{ id: 'p1', research_report: {
      parse_error: true, raw_output: 'x', raw_chars: 20442, raw_truncated: true,
    } }} gateNum="1" gatePhase="gate1" onGate={() => {}} />)
    expect(text).toContain('解析失败')
    expect(text, '又把锅甩给调研了 —— 报告明明写了推荐方案，只是解不开')
      .not.toContain('调研没给推荐方案')
  })

  it('报告正常时**还是原来那句**（别把修法改宽）', () => {
    const text = render(<GatePanel info={{ id: 'p1',
      research_report: { recommendation: '用 Python' } }} gateNum="1" gatePhase="gate1" onGate={() => {}} />)
    expect(text).toContain('推荐：用 Python')
    expect(text).not.toContain('解析失败')
  })
})

describe('GATE1 上看得见调研报告', () => {
  // 🔴 改之前：`GatePanel` 有 `gateNum !== '1'` 挡着，而 `Chat.tsx` 的 `!isGate`
  // 又把 `ProjectArchive` 藏了 ⇒ **两道门一起把报告挡死**。偏偏 GATE1 就是审调研的那道门
  // —— 用户在门上**一处都看不到报告**，只剩那条摘要。昨夜修的红字提示就修在这个不渲染的组件里。
  it('**GATE1 就要渲染调研报告**（删掉那道 `gateNum !== \'1\'` 挡板）', () => {
    const text = render(<GatePanel info={{ id: 'p1', research_report: { recommendation: '用 Python' } }}
                                   gateNum="1" gatePhase="gate1" onGate={() => {}} />)
    // ⚠️ 断言**报告内容**而不是标题字符串 —— 报告现在进「📋 调研」那一组、
    //    标题由分组提供（`bare`），盯标题会盯了个会搬家的东西。
    expect(text, 'GATE1 上看不到报告内容 —— 审调研的门上没有调研').toContain('用 Python')
  })

  it('**架构那组不许再套一层抽屉** —— 套了就要点两下', () => {
    // 用户 09-17 贴的截图：「🏗 架构 → 🏗 架构方案 → 点击展开」两层套娃。
    // 分组已经给了标题+摘要，里面的 `ArchitectureDetails` 就必须传 `bare`。
    //
    // ⚠️ **只钉架构，不钉调研**：调研组里确实有 8 个嵌套 `<details>`，
    //    但那是「每段一行」本身（用户点名要的功能），不是多余的壳。
    //    **"有嵌套"不是病，"套了个重复标题的壳"才是** —— 判据别写成前者。
    const el = dom(<GatePanel info={{ id: 'p1',
        architecture: { architecture: '主设计', modules: [], tasks: [] } }}
      gateNum="2" gatePhase="gate2" onGate={() => {}} />)
    const g = Array.from(el.querySelectorAll('details'))
      .find(d => (d.querySelector('summary')?.textContent || '').includes('🏗 架构'))
    expect(g, '没有「🏗 架构」这一组').toBeTruthy()
    expect(g!.querySelectorAll('details').length,
      '架构那组里还套着折叠框（🏗 架构方案）—— 要点两下才看得见内容').toBe(0)
    el.remove()
  })

  it('解析失败的原文一进来就要看得见（默认展开），不是再点一下', () => {
    const el = dom(<GatePanel info={{ id: 'p1', research_report: {
      parse_error: true, raw_output: '{"competitive_analysis"', raw_chars: 100,
    } }} gateNum="1" gatePhase="gate1" onGate={() => {}} />)
    const open = Array.from(el.querySelectorAll('details')).filter(d => d.hasAttribute('open'))
    expect(open.length, '解析失败的红框默认收起了 —— 用户还是看不到').toBeGreaterThan(0)
    el.remove()
  })
})

describe('打回时能写理由', () => {
  const type = (node: HTMLTextAreaElement, text: string) => {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, 'value')!.set!
    setter.call(node, text)
    node.dispatchEvent(new Event('input', { bubbles: true }))
  }
  const clickText = (el: HTMLElement, label: string) => {
    const btn = Array.from(el.querySelectorAll('button'))
      .find(b => (b.textContent || '').includes(label))
    expect(btn, `没找到「${label}」按钮`).toBeTruthy()
    act(() => { (btn as HTMLElement).click() })
  }
  const gate = (onGate: any) => dom(
    <GatePanel info={{ id: 'p1', research_report: { recommendation: 'x' } }}
               gateNum="1" gatePhase="gate1" onGate={onGate} />)

  it('点了打回**先弹出输入框**，不直接退回', () => {
    const got: any[] = []
    const el = gate((d: string, f?: string) => got.push([d, f]))
    clickText(el, '打回')
    expect(got, '一点打回就退了 —— 没给写理由的机会').toEqual([])
    expect(el.querySelector('textarea'), '没有输入框').toBeTruthy()
    el.remove()
  })

  it('**写的理由要传给 onGate**（删掉 `onGate(\'rejected\', reason)` 的第二个参数它会红）', () => {
    const got: any[] = []
    const el = gate((d: string, f?: string) => got.push([d, f]))
    clickText(el, '打回')
    type(el.querySelector('textarea') as HTMLTextAreaElement, '竞品太少，补到 5 家')
    clickText(el, '确认打回')
    expect(got).toEqual([['rejected', '竞品太少，补到 5 家']])
    el.remove()
  })

  it('**不写也能退** —— 选填，别让写理由变成打回的门槛', () => {
    const got: any[] = []
    const el = gate((d: string, f?: string) => got.push([d, f]))
    clickText(el, '打回')
    clickText(el, '确认打回')
    expect(got).toEqual([['rejected', '']])
    el.remove()
  })

  it('取消就什么都不发生', () => {
    const got: any[] = []
    const el = gate((d: string, f?: string) => got.push([d, f]))
    clickText(el, '打回')
    clickText(el, '取消')
    expect(got).toEqual([])
    expect(el.querySelector('textarea'), '取消后输入框还在').toBeFalsy()
    el.remove()
  })
})

// ═══════════════════════════════════════════════════════════════
// 架构的「历史版本」入口（2026-09-19）
// ═══════════════════════════════════════════════════════════════
// 归档早就落盘了（`_save_phase_output` 从 09-17 起覆盖前会存上一版），而界面上
// **一点入口都没有** —— 用户当场想看"打回前后的对比"。
// ⚠️ 这条钉的是**接线**：光有 `ArchitectureDetails` 里的链接不算，得证明
//   两个调用点真的把 `projectId` 传下去了（不传就是渲染个 href 里带 `undefined` 的死链）。
describe('架构历史版本入口', () => {
  const hrefs = (el: HTMLElement) =>
    Array.from(el.querySelectorAll('a')).map(a => a.getAttribute('href') || '')

  it('项目档案里给出了通往历史版本的链接', () => {
    const el = dom(<ProjectArchive info={{ id: 'p123', architecture: ARCH }} />)
    expect(hrefs(el), '历史版本的链接没渲染 —— 归档在盘上、界面上还是没入口')
      .toContain('/api/projects/p123/history/architecture.md')
    el.remove()
  })

  it('没有 projectId 时不给死链（href 里不许出现 undefined）', () => {
    const el = dom(<ProjectArchive info={{ architecture: ARCH }} />)
    expect(hrefs(el).some(h => h.includes('undefined')), '渲染出了带 undefined 的死链').toBe(false)
    el.remove()
  })
})
