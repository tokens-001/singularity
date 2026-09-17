// @vitest-environment jsdom
/**
 * `addChatMsg` 必须能**指定项目**（2026-09-17 补）。
 *
 * 原来它一律塞进 `get().activeProjectId` —— 也就是"用户此刻正开着的那个项目"。
 * 现在只有一个调用方（用户自己发消息，那确实该进当前项目），所以没暴露。
 * 但只要加**任何一处服务端主动推的消息**（观察者主动汇报、告警播报），
 * 就会当场串台：你在看 A 项目，B 项目的汇报插进 A 的对话里。
 *
 * ⚠️ 删掉 `addChatMsg(msg, projectId)` 的第二个参数、或把 `projectId || activeProjectId`
 * 写回 `get().activeProjectId`，第二条用例会红。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

// `persist` 中间件要 localStorage，而这个 jsdom 环境是 opaque origin、`localStorage`
// 是 undefined（直接 `setState` 会炸在 `setItem`）。给个内存替身，在 import **之前**装好
// —— `vi.hoisted` 的工厂跑在所有 import 前，普通顶层代码不行（import 会被提升）。
vi.hoisted(() => {
  const m: Record<string, string> = {}
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (k: string) => (k in m ? m[k] : null),
      setItem: (k: string, v: string) => { m[k] = String(v) },
      removeItem: (k: string) => { delete m[k] },
      clear: () => { for (const k of Object.keys(m)) delete m[k] },
    },
  })
})

import { useAppStore } from './app'

const msg = (c: string) => ({ role: 'assistant' as const, content: c, ts: 1 })

beforeEach(() => {
  useAppStore.setState({ conversations: {}, activeProjectId: 'A' })
})

describe('addChatMsg 落到哪个项目', () => {
  it('不给 projectId ⇒ 进当前打开的那个（旧行为，发消息靠它）', () => {
    useAppStore.getState().addChatMsg(msg('我在 A 说的话'))
    expect(useAppStore.getState().conversations['A']?.map(m => m.content))
      .toEqual(['我在 A 说的话'])
  })

  it('**给了 projectId ⇒ 进那个项目**，跟当前开着谁无关', () => {
    useAppStore.getState().addChatMsg(msg('B 项目的汇报'), 'B')
    const s = useAppStore.getState()
    expect(s.conversations['B']?.map(m => m.content), 'B 的汇报没进 B').toEqual(['B 项目的汇报'])
    expect(s.conversations['A'], 'B 的汇报串到 A 去了').toBeUndefined()
  })

  it('当前开着 B、却推给 A ⇒ 也进 A', () => {
    useAppStore.setState({ activeProjectId: 'B' })
    useAppStore.getState().addChatMsg(msg('给 A 的'), 'A')
    expect(useAppStore.getState().conversations['A']?.length).toBe(1)
    expect(useAppStore.getState().conversations['B']).toBeUndefined()
  })
})
