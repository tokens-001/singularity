import { describe, expect, it } from 'vitest'

import { shortId } from './ids'

describe('shortId', () => {
  it('同一批任务必须能分开 —— 这正是 slice(0,8) 坏掉的地方', () => {
    const a = '1789482513688'
    const b = '1789482513691'
    // 前提：旧写法在这两个真机任务号上确实撞车。撞不了，这条用例就没意义了。
    expect(a.slice(0, 8)).toBe(b.slice(0, 8))
    expect(shortId(a)).not.toBe(shortId(b))
  })

  it('短于 8 位的原样返回，不补不炸', () => {
    expect(shortId('abc')).toBe('abc')
    expect(shortId('')).toBe('')
  })
})
