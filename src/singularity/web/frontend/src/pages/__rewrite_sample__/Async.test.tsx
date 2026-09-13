// @vitest-environment jsdom
/**
 * Async 四态边界 —— 锁住原语本身的判据，消费者（McpTab 等）另测。
 *
 * 钉四件事：
 * ① 四态各有文字，加载中/失败绝不渲染空态文案（"读不到"≠"没有"）
 * ② 失败态有重试出口，重试能恢复
 * ③ 有数据后再取失败：旧数据**保留**、失败**可见** —— 两头都不许说谎
 * ④ 空态判据是 `length === 0`，不是真值与否
 *
 * harness 照抄 AgentsTab.test.tsx（本项目没有 @testing-library/react，
 * 裸 createRoot + act）。
 */
import { describe, it, expect, beforeAll } from 'vitest'
import { act, useEffect, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { AsyncBoundary, useResource } from './Async'

beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
})

/** 可控的 fetcher：每个用例自己决定 resolve / reject。 */
let resolveFetch!: (v: string[]) => void
let rejectFetch!: (e: unknown) => void

function Probe({ reloadLabel }: { reloadLabel?: boolean }) {
  const { state, load } = useResource<string[]>(
    () =>
      new Promise<string[]>((res, rej) => {
        resolveFetch = res
        rejectFetch = rej
      }),
    { errorLabel: '加载失败' }
  )
  useEffect(() => { void load(true) }, [load])
  return (
    <div>
      {reloadLabel && <button onClick={() => void load(false)}>reload</button>}
      <AsyncBoundary
        state={state}
        loadingText="加载中文字"
        emptyText="空态文字"
        emptyHint="空态提示"
        onRetry={() => void load(true)}
      >
        {(data) => <div>数据 {data.join(',')}</div>}
      </AsyncBoundary>
    </div>
  )
}

async function render(node: ReactNode): Promise<HTMLElement> {
  const el = document.createElement('div')
  document.body.appendChild(el)
  await act(async () => { createRoot(el).render(node) })
  await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  return el
}

describe('AsyncBoundary 四态', () => {
  it('加载中有文字，且不显示空态文案', async () => {
    const el = await render(<Probe />)
    expect(el.textContent).toContain('加载中文字')
    expect(el.textContent).not.toContain('空态文字')
  })

  it('有数据渲染正文', async () => {
    const el = await render(<Probe />)
    await act(async () => { resolveFetch(['a', 'b']) })
    expect(el.textContent).toContain('数据 a,b')
    expect(el.textContent).not.toContain('加载中文字')
  })

  it('空数组渲染空态文字 + 提示 —— 判据是 length===0，不是真值', async () => {
    const el = await render(<Probe />)
    await act(async () => { resolveFetch([]) })
    expect(el.textContent).toContain('空态文字')
    expect(el.textContent).toContain('空态提示')
    expect(el.textContent).not.toContain('加载中文字')
  })

  it('失败是自己的态：显示原因和重试，绝不落进空态文案', async () => {
    const el = await render(<Probe />)
    await act(async () => { rejectFetch(new Error('后端说了原因')) })
    const text = el.textContent || ''
    expect(text).toContain('加载失败：后端说了原因')
    expect(text).not.toContain('空态文字')
    expect(el.querySelector('button')?.textContent).toContain('重试')
  })

  it('重试能从失败恢复', async () => {
    const el = await render(<Probe />)
    await act(async () => { rejectFetch(new Error('第一次失败')) })
    expect(el.textContent).toContain('第一次失败')
    await act(async () => { el.querySelector('button')!.click() })
    await act(async () => { resolveFetch(['x']) })
    expect(el.textContent).toContain('数据 x')
    expect(el.textContent).not.toContain('第一次失败')
  })

  it('有数据后再取失败：旧数据保留 + 失败可见（两头都不许说谎）', async () => {
    const el = await render(<Probe reloadLabel />)
    await act(async () => { resolveFetch(['旧数据']) })

    await act(async () => { el.querySelectorAll('button')[0]!.click() })   // reload（initial=false）
    await act(async () => { rejectFetch(new Error('网络断了')) })

    const text = el.textContent || ''
    expect(text).toContain('数据 旧数据')        // 旧数据没被吹掉
    expect(text).toContain('刷新失败：网络断了')  // 失败也没被 toast 一声就吞掉
    expect(text).toContain('最近一次成功加载的数据')
  })

  it('再取成功后失败条消失', async () => {
    const el = await render(<Probe reloadLabel />)
    await act(async () => { resolveFetch(['旧数据']) })
    await act(async () => { el.querySelectorAll('button')[0]!.click() })
    await act(async () => { rejectFetch(new Error('断')) })
    expect(el.textContent).toContain('刷新失败')

    await act(async () => { el.querySelectorAll('button')[0]!.click() })
    await act(async () => { resolveFetch(['新数据']) })
    expect(el.textContent).toContain('数据 新数据')
    expect(el.textContent).not.toContain('刷新失败')
  })
})

// ═══════════════════════════════════════════════════════════════
// 并发（2026-09-14 外派⑧反审抓到的**地基裂缝**：原语第一版没有请求序号）
// ═══════════════════════════════════════════════════════════════
// 这个前端"挂载 / SSE 再取 / 轮询 / 手动刷新"三路并发是常态（dev 下 StrictMode
// 每次挂载就真有两路）。没有序号时**两个方向都会错**，而且第二个方向是**新增的谎**：
// 慢的失败后到 → 往新成功的数据头上挂「刷新失败」，而那条横幅写着
// "下面还是最近一次成功加载的数据" —— 屏幕上恰恰就是最新数据。
//
// 这组用**可分辨先后**的 fetcher：每次调用把 (resolve, reject) 排进队列，
// 于是能独立控制"第 1 个请求"和"第 2 个请求"。

let pending: { resolve: (v: string[]) => void; reject: (e: unknown) => void }[] = []

function ProbeSeq() {
  const { state, load } = useResource<string[]>(
    () => new Promise<string[]>((res, rej) => { pending.push({ resolve: res, reject: rej }) }),
    { errorLabel: '加载失败' }
  )
  useEffect(() => { void load(true) }, [load])
  return (
    <div>
      <button onClick={() => void load(false)}>reload</button>
      <AsyncBoundary state={state} loadingText="加载中文字" emptyText="空态文字"
        onRetry={() => void load(true)}>
        {(data) => <div>数据 {data.join(',')}</div>}
      </AsyncBoundary>
    </div>
  )
}

describe('并发：后到的不一定算数', () => {
  it('慢的先发后到，不许盖掉新的', async () => {
    pending = []
    const el = await render(<ProbeSeq />)
    await act(async () => { el.querySelectorAll('button')[0]!.click() })   // 第 2 个请求
    expect(pending.length, '应该有两个在飞的请求').toBe(2)

    await act(async () => { pending[1].resolve(['新']) })                  // 新的先回
    await act(async () => { pending[0].resolve(['旧']) })                  // 旧的后回

    expect(el.textContent).toContain('数据 新')
    expect(el.textContent, '慢请求后到把新数据盖掉了 —— 缺请求序号').not.toContain('数据 旧')
  })

  it('旧的**失败**后到，不许往新数据上贴一条假的「刷新失败」', async () => {
    pending = []
    const el = await render(<ProbeSeq />)
    await act(async () => { el.querySelectorAll('button')[0]!.click() })
    await act(async () => { pending[1].resolve(['新']) })
    await act(async () => { pending[0].reject(new Error('旧请求炸了')) })

    expect(el.textContent).toContain('数据 新')
    expect(el.textContent,
      '过期的失败贴了「刷新失败」—— 那条横幅说"下面是最近一次成功加载的数据"，'
      + '而屏幕上就是最新数据 ⇒ 这是新造出来的谎').not.toContain('刷新失败')
  })

  it('对照：最新那次失败，仍然要可见（别把序号修成"什么都不报"）', async () => {
    pending = []
    const el = await render(<ProbeSeq />)
    await act(async () => { pending[0].resolve(['旧']) })
    await act(async () => { el.querySelectorAll('button')[0]!.click() })
    await act(async () => { pending[1].reject(new Error('真的断了')) })

    expect(el.textContent).toContain('数据 旧')
    expect(el.textContent).toContain('刷新失败：真的断了')
  })
})
