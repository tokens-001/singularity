// @vitest-environment jsdom
/**
 * 任务卡的验收结论渲染 —— 锁住"**没通过绝不显示成对勾**"。
 *
 * 原来的判据是 `t.verdict && t.verdict !== '?'`，样式写死绿色 + `✓`。
 * 而后端 `verdict` 的取值是 `validator.py` 里那几个
 * （`通过` / `阻断` / `gate失败` / `信息不足` / `人工复核` / `未知`）
 * ⇒ 任务**被阻断**、门禁**失败**时，卡片上是绿色的「✓ 阻断」。
 * 判据落在"非空"而不是"成功"上，默认那一侧是好消息 ——
 * **和 `GatePanel` 那条 GATE3 绿灯是同一个病**（见 `GatePanel.test.tsx`）。
 *
 * harness 照抄 `GatePanel.test.tsx`（裸 createRoot + act）。
 */
import { describe, it, expect } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { TaskCard, taskStateKind } from './TaskCard'

function render(node: ReactNode): string {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const root = createRoot(el)
  act(() => { root.render(node) })
  const text = el.textContent || ''
  act(() => { root.unmount() })
  return text
}

const base = { id: 't1', desc: '一个任务', status: 'done', ts: 0, files: ['a.ts'] }
const noop = () => {}

function card(verdict: string) {
  return render(<TaskCard t={{ ...base, verdict }} onRetry={noop} onReveal={noop} />)
}

/** 按**状态**渲染（上面的 `card` 传的是 verdict，别混）。 */
function cardWithStatus(status: string) {
  return render(<TaskCard t={{ ...base, status }} onRetry={noop} onReveal={noop} />)
}

describe('验收结论的三态渲染', () => {
  it('「阻断」不许渲染成对勾', () => {
    const text = card('阻断')
    expect(text).toContain('阻断')
    expect(text, '被阻断的任务渲染成了绿色的对勾 —— 判据又落回"非空"了').not.toContain('✓ 阻断')
  })

  it('「gate失败」不许渲染成对勾', () => {
    expect(card('gate失败')).not.toContain('✓ gate失败')
  })

  it('「通过」才是对勾', () => {
    expect(card('通过')).toContain('✓ 通过')
  })

  it('「人工复核」「信息不足」走"待人工"，不是对勾', () => {
    expect(card('人工复核')).not.toContain('✓')
    expect(card('信息不足')).not.toContain('✓')
  })

  it('**不认识的档也不许落到绿** —— 将来后端加了新 verdict，默认那侧必须不是"通过"', () => {
    expect(card('某个以后才有的档')).not.toContain('✓')
  })

  it('没结论时不渲染（灰 = 没数据）', () => {
    const text = card('')
    expect(text).not.toContain('✓')
    expect(text).not.toContain('✕')
  })
})


describe('任务状态的十二个取值', () => {
  it('终态 rolled_back 不许画成"在跑"', () => {
    // 后端 TaskStatus 12 个值里 `rolled_back` 是**终态**，
    // 而卡片原来只认 done/failed/cancelled ⇒ 它落进"执行中"+永转的圈。
    expect(taskStateKind('rolled_back')).toBe('failed')
  })

  it('等人的三个状态都不算"在跑"', () => {
    for (const st of ['paused', 'conflict_held', 'blocked']) {
      expect(taskStateKind(st), st).toBe('waiting')
    }
  })

  it('真正进行中的六个仍是 running', () => {
    for (const st of ['pending', 'routed', 'dispatched', 'running', 'validating', 'decomposed']) {
      expect(taskStateKind(st), st).toBe('running')
    }
  })

  it('**不认识的新状态一律落"停"，不许落"在跑"**', () => {
    // 后端加了状态而前端没跟上时，把停住的东西画成还在转比画错颜色坏得多
    expect(taskStateKind('某个以后才有的状态')).toBe('waiting')
  })

  it('渲染出来的标签也跟档位走（退回旧实现会红）', () => {
    expect(cardWithStatus('rolled_back')).toContain('失败')
    expect(cardWithStatus('rolled_back')).not.toContain('执行中')
    expect(cardWithStatus('paused')).toContain('暂停/等待')
    expect(cardWithStatus('paused')).not.toContain('执行中')
    // 对照：真在跑的还是"执行中"（别把闸门修成"什么都不动"）
    expect(cardWithStatus('running')).toContain('执行中')
    expect(cardWithStatus('done')).toContain('完成')
  })
})

/**
 * 🔴 **F3：判失败 ≠ 活白干**（2026-09-17 真机）。
 *
 * 任务判 `failed` 之后，它的产物**不一定丢** —— executor 干完一轮会 `commit_wt`
 * 并把提交**锚在 `refs/qidian/pending/<task_id>`** 上（防 git gc）。
 * 成功合并那条路会把它删掉 ⇒ **ref 还在 = 有可打捞的产物**。
 *
 * 真机那轮：3 个任务全判 `failed`，而产物好好躺在 pending ref 上
 * （拼起来 `pytest 40 passed`）—— **界面上一个字都不显示**，
 * 用户只看到"失败"，不知道活其实干完了。
 */
describe('任务卡上要看得见"有可打捞的产物"', () => {
  it('有 salvage_ref 时要说出来', () => {
    const text = render(
      <TaskCard t={{ ...base, status: 'failed', error: 'QA: 无文件改动',
                     salvage_ref: '1974746abcdef' }} onRetry={noop} onReveal={noop} />)
    expect(text).toContain('可打捞')
  })

  it('**没有产物时一个字都不许提**（别把修法改宽）', () => {
    const text = render(
      <TaskCard t={{ ...base, status: 'failed', error: 'QA: 无文件改动' }}
                onRetry={noop} onReveal={noop} />)
    expect(text).not.toContain('可打捞')
  })
})
