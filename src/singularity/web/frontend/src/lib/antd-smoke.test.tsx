// @vitest-environment jsdom
// 冒烟测试：build 全绿证明不了运行时没坏。这里真渲染一遍，覆盖两件最容易静默失效的事：
// ① App.useApp().message 是否真出 DOM（静态 message 会读不到 ConfigProvider 的 theme）
// ② Bubble/Sender 能否脱离 useXChat 数据层单独当纯 UI 用
import { describe, it, expect, beforeAll } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { App as AntApp, ConfigProvider } from 'antd'
import { Bubble, Sender } from '@ant-design/x'
import { antdTheme } from './theme'

beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
  // jsdom 没有 ResizeObserver，antd 的 Bubble/Sender 依赖它（真实浏览器里存在）
  g.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})

function Probe() {
  const { message } = AntApp.useApp()
  return <button onClick={() => message.success('保存成功')}>go</button>
}

async function render(node: ReactNode) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  await act(async () => { createRoot(el).render(node) })
  await act(async () => { await new Promise((r) => setTimeout(r, 0)) })  // 冲掉 antd 动效的延迟状态更新
  return el
}

describe('antd 接入冒烟', () => {
  it('App.useApp().message 真渲染，且 colorPrimary 进了 CSS-in-JS', async () => {
    const el = await render(
      <ConfigProvider theme={antdTheme}><AntApp><Probe /></AntApp></ConfigProvider>
    )
    await act(async () => { el.querySelector('button')!.click() })
    expect(document.querySelector('.ant-message')?.textContent).toContain('保存成功')
    const css = Array.from(document.querySelectorAll('style')).map(s => s.textContent || '').join('')
    expect(css).toContain('#2563eb')
  })

  it('Bubble / Sender 能独立渲染（不依赖 useXChat）', async () => {
    const el = await render(
      <ConfigProvider theme={antdTheme}>
        <AntApp>
          <Bubble placement="end" content="你好" />
          <Sender value="hi" onSubmit={() => {}} />
        </AntApp>
      </ConfigProvider>
    )
    expect(el.querySelector('.ant-bubble')?.textContent).toContain('你好')
    expect(el.querySelector('.ant-sender')).toBeTruthy()
  })
})
