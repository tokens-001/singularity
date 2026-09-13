// @vitest-environment jsdom
/**
 * 告警页渲染测试 —— 锁住**"常驻"和"事件"真的分在两栏**。
 *
 * 这一页的全部意义就是那个拆分：后端已经把 `chronic` 算好了（`n >= chronic_min`），
 * 前端要是把两组混在一起渲染，页面看着照样"有内容"，但**常驻会被事件淹掉** ——
 * 正是当初造这个聚合视图要解决的问题，等于白做。
 *
 * harness 照抄 `components/chat/GatePanel.test.tsx`（裸 createRoot + act）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act } from 'react'
import { createRoot } from 'react-dom/client'

vi.mock('../lib/api', () => ({ api: { status: vi.fn() } }))
vi.mock('../lib/toast', () => ({ useToast: () => () => {} }))

import { api } from '../lib/api'
import Alerts from './Alerts'

const g = (over: any) => ({
  key: 'k', scopes: ['oa_exec'], n: 1, first_ts: 1789300000, last_ts: 1789300000,
  chronic: false, sample: '样例', ...over,
})

async function renderAlerts(groups: any[]): Promise<string> {
  ;(api.status as any).mockResolvedValue({ alert_summary: groups })
  const el = document.createElement('div')
  document.body.appendChild(el)
  const root = createRoot(el)
  await act(async () => { root.render(<Alerts />) })
  const text = el.textContent || ''
  act(() => { root.unmount() })
  el.remove()
  return text
}

beforeEach(() => { vi.clearAllMocks() })

describe('告警页', () => {
  it('常驻和后端标 chronic 的那组对上，事件在对的那一栏', async () => {
    const text = await renderAlerts([
      g({ key: 'collect_changes', n: 57, chronic: true }),
      g({ key: 'constraints_checklist_fallback', n: 10, chronic: true }),
      g({ key: '偶发那个', n: 1, chronic: false }),
    ])

    // 用「事件」这个小标题当分界线：它只出现在那一栏（常驻那栏的说明文字里没有"事件"二字）
    const cut = text.indexOf('事件')
    expect(cut, '两次分栏的小标题都没渲染出来').toBeGreaterThan(-1)

    // ⚠️ 必须**切开来查**，不能只查 `indexOf(k) < cut`。
    // 只查"第一次出现的位置"的话，一条 key **同时出现在两栏**也照样绿
    // （实测：把事件那栏的 chronic 过滤删掉，那种写法不变红 —— 假绿）。
    const before = text.slice(0, cut), after = text.slice(cut)
    for (const k of ['collect_changes', 'constraints_checklist_fallback']) {
      expect(before, `常驻 ${k} 没渲染在常驻栏`).toContain(k)
      expect(after, `${k} 漏进「事件」栏了 —— 常驻又被事件淹了`).not.toContain(k)
    }
    expect(after, '事件被渲染到「常驻」那栏去了').toContain('偶发那个')
    expect(before, '事件混进常驻栏了').not.toContain('偶发那个')
  })

  it('没有常驻时那一栏说"没有"，不是空着', async () => {
    const text = await renderAlerts([g({ key: '偶发那个', n: 1, chronic: false })])
    expect(text).toContain('没有常驻告警')
    expect(text).toContain('偶发那个')
  })

  it('一条告警都没有时两栏都要说话', async () => {
    const text = await renderAlerts([])
    expect(text).toContain('没有常驻告警')
    expect(text).toContain('没有事件级告警')
  })

  it('后端没给 alert_summary（老后端）时按空处理，不能崩', async () => {
    const text = await renderAlerts(undefined as any)
    expect(text).toContain('没有常驻告警')
  })
})
