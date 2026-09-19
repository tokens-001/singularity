/**
 * 架构图的**分层面板**。
 *
 * 只钉纯函数（`computeLayers` / `layout` / `truncate`），不渲染 React ——
 * 要防的三件事都是"画不出来"级别的，跟 DOM 无关：
 *  ① **环**：架构是模型出的，`depends_on` 不保证无环。层号迭代定不下来时若没有兜底，
 *     要么死循环、要么 `depth[id]` 是 `undefined` ⇒ 一整张图（含没环的部分）都不显示。
 *  ② **指向不存在节点的依赖**：架构里指到一个没列出来的模块/任务，不能连带炸掉布局。
 *  ③ **标签顶出盒子**：中文按 `len()` 算会少算一半宽度（`truncate` 那条）。
 */
import { describe, it, expect } from 'vitest'
import { computeLayers, layout, truncate } from './ArchGraph'

describe('computeLayers', () => {
  it('链式依赖逐层递进', () => {
    const { depth, maxDepth } = computeLayers([
      { id: 'A' },
      { id: 'B', deps: ['A'] },
      { id: 'C', deps: ['B'] },
    ])
    expect(depth).toEqual({ A: 0, B: 1, C: 2 })
    expect(maxDepth).toBe(2)
  })

  it('菱形取最深的那条路，不是随便挑一个依赖', () => {
    // A→B→D 与 A→C→D：D 必须是 2。挑错的话 D 会跟 B/C 同层、箭头往回画。
    const { depth } = computeLayers([
      { id: 'A' },
      { id: 'B', deps: ['A'] },
      { id: 'C', deps: ['A'] },
      { id: 'D', deps: ['B', 'C'] },
    ])
    expect(depth).toEqual({ A: 0, B: 1, C: 1, D: 2 })
  })

  it('成环不挂，且每个节点都拿到层号（图的其余部分照画）', () => {
    const { depth, orphans } = computeLayers([
      { id: 'A', deps: ['B'] },
      { id: 'B', deps: ['A'] },
      { id: 'C' },
    ])
    expect(orphans.sort()).toEqual(['A', 'B'])
    expect(Object.keys(depth).sort()).toEqual(['A', 'B', 'C'])
    expect(depth.C).toBe(0)
  })

  it('自环当作没有依赖', () => {
    expect(computeLayers([{ id: 'A', deps: ['A'] }]).depth).toEqual({ A: 0 })
  })

  it('指向不存在节点的依赖被忽略，不算层级也不算孤儿', () => {
    const { depth, orphans } = computeLayers([{ id: 'A', deps: ['幽灵'] }])
    expect(depth).toEqual({ A: 0 })
    expect(orphans).toEqual([])
  })
})

describe('layout', () => {
  it('每个节点都有坐标，且框在画布内', () => {
    const nodes = [
      { id: 'A' },
      { id: 'B', deps: ['A'] },
      { id: 'C', deps: ['A'] },
      { id: 'D', deps: ['B', 'C'] },
    ]
    const { pos, width, height } = layout(nodes)
    for (const n of nodes) {
      expect(pos[n.id]).toBeDefined()
      expect(pos[n.id].x).toBeGreaterThanOrEqual(0)
      expect(pos[n.id].x + 132).toBeLessThanOrEqual(width + 0.01)
    }
    expect(height).toBeGreaterThan(0)
    // 同层不重叠：B 与 C 同层且 x 必须分开
    expect(pos.C.x - pos.B.x).toBeGreaterThanOrEqual(132)
  })
})

describe('truncate', () => {
  it('短标签原样', () => {
    expect(truncate('parse', 16)).toBe('parse')
  })

  it('中文按 2 格算宽度（按字符数算会顶出盒子）', () => {
    // 9 个汉字 = 18 格 > 16 ⇒ 必截。若按 len() 只有 9 就不会截。
    const s = '脚手架与共享类型契约'
    expect(dispLenOf(s)).toBeGreaterThan(16)
    expect(truncate(s, 16).endsWith('…')).toBe(true)
  })
})

/** 测试自带的宽度算法复算：故意不复用实现里的私有函数，免得"实现错、测试跟着错"。 */
const dispLenOf = (s: string) => [...s].reduce((n, c) => n + (c.charCodeAt(0) > 0x2e80 ? 2 : 1), 0)
