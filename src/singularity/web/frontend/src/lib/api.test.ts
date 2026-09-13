// ponytail: 确保 API 对象结构完整，不发无效请求
import { describe, it, expect } from 'vitest'
import { rejectErrorPlaceholder } from './api'
import { api } from './api'

describe('api', () => {
  it('has all expected methods', () => {
    expect(typeof api.status).toBe('function')
    expect(typeof api.tasks).toBe('function')
    expect(typeof api.projects).toBe('function')
    expect(typeof api.agents).toBe('function')
    expect(typeof api.models).toBe('function')
    expect(typeof api.skills).toBe('function')
    expect(typeof api.apiStore).toBe('function')
    expect(typeof api.observerChat).toBe('function')
  })

  it('task operations exist', () => {
    expect(typeof api.createTask).toBe('function')
    expect(typeof api.cancelTask).toBe('function')
    expect(typeof api.retryTask).toBe('function')
    expect(typeof api.holdTask).toBe('function')
    expect(typeof api.releaseTask).toBe('function')
  })

  it('model operations exist', () => {
    expect(typeof api.addModel).toBe('function')
    expect(typeof api.deleteModel).toBe('function')
    expect(typeof api.importModels).toBe('function')
    expect(typeof api.scanApiStore).toBe('function')
  })

  it('skill operations exist', () => {
    expect(typeof api.addSkill).toBe('function')
    expect(typeof api.deleteSkill).toBe('function')
    expect(typeof api.agentSkills).toBe('function')
    expect(typeof api.updateAgentSkills).toBe('function')
  })
})

// ── 200 + `{error}` 占位符不许被当成正常数据 ──────────────────────────
// 后端有一类形状是"请求成功、内容其实是'读不到'"（如 `/api/files/diff`）。
// 而 `tasks` / `models` / `apiStore` 的归一化是 `(d?.tasks||d)` / `Object.values(d||{})`
// —— **占位符对象是真值**，会当成正常数据往下走：前者让调用方 `.filter()` 崩，
// 后者把一句错误文案渲染成"一条数据"（2026-09-14，外派④扫前端抓出）。

describe('rejectErrorPlaceholder', () => {
  it('`{error}` 占位符要**抛**，不许当数据返回', () => {
    expect(() => rejectErrorPlaceholder({ error: '加载失败：磁盘读不到' }))
      .toThrow('加载失败：磁盘读不到')   // 中文原因直接透出来（和非 2xx 那条路一致）
  })

  it('正常对象原样返回（别把正常路径也堵了）', () => {
    const d = { tasks: [{ id: 't1' }] }
    expect(rejectErrorPlaceholder(d)).toBe(d)
  })

  it('数组原样返回（数组有自己的 `error` 字段也不能误伤）', () => {
    const a = [{ error: '这是数据里的一个字段，不是占位符' }]
    expect(rejectErrorPlaceholder(a)).toBe(a)
  })

  it('空字符串的 error 不算占位符', () => {
    const d = { error: '' }
    expect(rejectErrorPlaceholder(d)).toBe(d)
  })
})
