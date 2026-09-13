// @vitest-environment jsdom
/**
 * McpTab 样板的渲染判据测试。
 *
 * 钉住的核心一条（也是原版踩中的雷）：
 *   **加载失败绝不渲染成「没有 MCP 服务器」** —— 失败有自己的态、自己的文字。
 *   （原版 catch 里只 toast，`servers` 停在 []，空态文案照常渲染 —— 把"读不到"
 *    说成了"没有"。下面第一个 error 用例就是这条的回归锁。）
 *
 * 外加字段判据逐条锁（完整判据表见本目录 README.md）：
 *   name 空 → （未命名） · command/url 全空 → 未配置启动方式 ·
 *   connected 缺 → 状态未知（不许落灰"未连接"） · enabled=false → 已停用 ·
 *   tool_count 缺 → 工具数未知（不许落 0） · transport 生词 → 原样回显
 *
 * harness 照抄 AgentsTab.test.tsx：裸 createRoot + act，真实 toast（包 AntApp），
 * 只 mock lib/api。路径注意：样板在 pages/__rewrite_sample__/ 下，lib 在上两级。
 */
import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest'
import { act, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { App as AntApp } from 'antd'

vi.mock('../../lib/api', () => ({
  api: {
    mcpServers: vi.fn(),
    mcpTools: vi.fn(),
    mcpRefresh: vi.fn(),
  },
}))

import { api } from '../../lib/api'
import McpTab from './McpTab'

beforeAll(() => {
  const g = globalThis as any
  g.IS_REACT_ACT_ENVIRONMENT = true
})
beforeEach(() => { vi.clearAllMocks() })

/** 后端契约形状（_api_admin.py mcp_server_list）：8 键恒给，值可能空串。 */
const server = (over: Record<string, unknown> = {}) => ({
  name: 'fs-server', transport: 'stdio', command: 'npx -y mcp-fs', url: '',
  enabled: true, connected: true, tool_count: 3, ...over,
})

function mockLoad(servers: unknown, tools: unknown[] = [{ name: 'mcp__fs__read' }]) {
  ;(api.mcpServers as any).mockResolvedValue(servers)
  ;(api.mcpTools as any).mockResolvedValue(tools)
}

async function flush() {
  await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

/** 按可见文案找按钮再点 —— NodeList 没有 .find，得走 Array.from。 */
async function click(el: HTMLElement, label: string) {
  const btn = Array.from(el.querySelectorAll('button') as NodeListOf<HTMLButtonElement>)
    .find((b) => b.textContent?.includes(label))
  expect(btn, `找不到按钮「${label}」`).toBeTruthy()
  await act(async () => { btn!.click() })
}

async function renderTab(): Promise<HTMLElement> {
  const el = document.createElement('div')
  document.body.appendChild(el)
  await act(async () => { createRoot(el).render(<AntApp><McpTab /></AntApp>) })
  await flush()
  return el
}

async function renderWith(servers: unknown, tools: unknown[] = [{ name: 't' }]): Promise<HTMLElement> {
  mockLoad(servers, tools)
  return renderTab()
}

describe('McpTab 四态', () => {
  it('加载中有文字；头部**不显示计数**（这时显示 (0) 是编的）', async () => {
    ;(api.mcpServers as any).mockImplementation(() => new Promise(() => {}))
    ;(api.mcpTools as any).mockImplementation(() => new Promise(() => {}))
    const el = await renderTab()
    const text = el.textContent || ''
    expect(text).toContain('正在加载 MCP 服务器')
    expect(text, '加载中就把 (0) 摆出来 = 把"还没数"说成"数过是零"').not.toContain('（0）')
    expect(text).not.toContain('没有配置任何')
  })

  it('空态：说了"没有"，还说了下一步', async () => {
    const el = await renderWith([], [])
    const text = el.textContent || ''
    expect(text).toContain('没有配置任何 MCP 服务器')
    expect(text).toContain('mcp.toml')
    // 空态的 (0) 是真的数过 —— 必须显示（和加载中的"没数"区分开）
    expect(text).toContain('（0）')
  })

  it('🔴 加载失败绝不渲染成「没有 MCP 服务器」—— 原版的雷', async () => {
    ;(api.mcpServers as any).mockRejectedValue(new Error('toml 读不出来'))
    ;(api.mcpTools as any).mockResolvedValue([])
    const el = await renderTab()
    const text = el.textContent || ''
    expect(text).toContain('加载 MCP 服务器失败：toml 读不出来')
    expect(text, '失败被渲染成了空态 —— "读不到"说成了"没有"').not.toContain('没有配置任何 MCP 服务器')
    expect(text, '失败时头部摆 (0) 同样是编的').not.toContain('（0）')
    const retry = Array.from(el.querySelectorAll('button') as NodeListOf<HTMLButtonElement>)
      .find((b) => b.textContent?.includes('重试'))
    expect(retry?.textContent).toContain('重试')
  })

  it('失败后重试能恢复', async () => {
    ;(api.mcpServers as any).mockRejectedValueOnce(new Error('后端没起')).mockResolvedValueOnce([server()])
    ;(api.mcpTools as any).mockResolvedValue([])
    const el = await renderTab()
    expect(el.textContent).toContain('后端没起')
    await act(async () => {
      click(el, '重试')
    })
    await flush()
    expect(el.textContent).toContain('fs-server')
    expect(el.textContent).not.toContain('后端没起')
  })

  it('成功态：计数、连接状态、启动方式都如实显示', async () => {
    const el = await renderWith([
      server(),
      server({ name: 'web-server', transport: 'http', command: '', url: 'http://127.0.0.1:9000', connected: false, tool_count: 0 }),
    ])
    const text = el.textContent || ''
    expect(text).toContain('MCP 服务器（2）')
    expect(text).toContain('1 个工具')
    expect(text).toContain('3 工具')          // connected + tool_count=3
    expect(text).toContain('npx -y mcp-fs')   // stdio 的 command
    expect(text).toContain('未连接')
    expect(text).toContain('http://127.0.0.1:9000') // http 的 url
  })

  it('有数据后再取失败：旧数据保留 + 失败可见', async () => {
    mockLoad([server()])
    const el = await renderTab()
    ;(api.mcpRefresh as any).mockRejectedValue(new Error('refresh 炸了'))
    ;(api.mcpServers as any).mockRejectedValue(new Error('列表也炸了'))
    await act(async () => {
      click(el, '重新加载配置')
    })
    await flush()
    const text = el.textContent || ''
    expect(text).toContain('fs-server')            // 旧数据没被吹掉
    expect(text).toContain('刷新失败')              // 失败也没被吞
    expect(text).toContain('列表也炸了')
  })
})

describe('McpTab 字段判据（每条缺值都有说法）', () => {
  it('name 空串 → （未命名），不许渲染成看不见的空行', async () => {
    const el = await renderWith([server({ name: '' })])
    expect(el.textContent).toContain('（未命名）')
  })

  it('command 和 url 全空 → 说出"未配置启动方式"', async () => {
    const el = await renderWith([server({ command: '', url: '' })])
    expect(el.textContent).toContain('未配置启动方式')
  })

  it('connected 缺失 → 状态未知（琥珀），不许落灰"未连接"', async () => {
    const el = await renderWith([server({ connected: undefined, tool_count: undefined })])
    const text = el.textContent || ''
    expect(text).toContain('状态未知')
    expect(text, '"不知道连没连"被说成了"没连上"').not.toContain('未连接')
  })

  it('enabled=false → 已停用（不是"未连接"—— 停用是人的决定，不是故障）', async () => {
    const el = await renderWith([server({ enabled: false, connected: false, tool_count: 0 })])
    const text = el.textContent || ''
    expect(text).toContain('已停用')
    expect(text).not.toContain('未连接')
  })

  it('connected=true 但 tool_count 缺失 → 工具数未知，不许落 0', async () => {
    const el = await renderWith([server({ tool_count: undefined })])
    expect(el.textContent).toContain('工具数未知')
  })

  it('transport 是不认识的词 → 原样回显（不认识的枚举值 ≠ 空）', async () => {
    const el = await renderWith([server({ transport: 'carrier-pigeon' })])
    expect(el.textContent).toContain('carrier-pigeon')
  })

  it('enabled=false 却显示已连接 → 配置矛盾（琥珀），不静默选一边', async () => {
    const el = await renderWith([server({ enabled: false, connected: true })])
    expect(el.textContent).toContain('配置矛盾')
  })

  it('服务器都在但 0 个工具 → 0 要解释它意味着什么', async () => {
    const el = await renderWith([server({ connected: false, tool_count: 0 })], [])
    expect(el.textContent).toContain('0 个工具')
    expect(el.textContent).toContain('没有一个连接成功')
  })
})
