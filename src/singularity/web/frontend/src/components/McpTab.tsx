import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { useRun, useToast } from '../lib/toast'
import { RefreshCw } from 'lucide-react'

/** MCP 服务器（只读列表 + 重新加载配置）。
 *
 * 加/删服务器仍是手改 mcp.toml；这里解决的是「改完不用重启」——
 * 后端启动时只装载一次配置，/api/mcp/refresh 是唯一的重载入口。
 */
export default function McpTab() {
  const [servers, setServers] = useState<any[]>([])
  const [tools, setTools] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const run = useRun()
  const toast = useToast()

  const fetch = async () => {
    setLoading(true)
    try {
      const [s, t] = await Promise.all([api.mcpServers(), api.mcpTools()])
      setServers(s); setTools(t)
    } catch { toast('加载 MCP 服务器失败', 'error') }
    setLoading(false)
  }
  useEffect(() => { fetch() }, [])

  const refresh = async () => {
    if (!(await run(() => api.mcpRefresh()))) return
    fetch()
  }

  return (
    <div>
      <div className="flex-center gap-8" style={{ marginBottom: 8 }}>
        <span className="fw-600 fs-12 text-secondary">MCP 服务器 ({servers.length})</span>
        <span className="fs-10 text-muted">{tools.length} 个工具</span>
        <span className="flex-1"/>
        <button onClick={refresh} className="btn-sm" title="重新读取 mcp.toml 并装载，不用重启后端">
          <RefreshCw size={12}/> 重新加载配置
        </button>
      </div>
      {loading ? (
        <div className="fs-11 text-muted" style={{ padding: 20, textAlign: 'center' }}>加载中...</div>
      ) : servers.length === 0 ? (
        <div className="fs-11 text-muted" style={{ padding: 20, textAlign: 'center' }}>
          没有 MCP 服务器。编辑 mcp.toml 后点「重新加载配置」即可生效，无需重启。
        </div>
      ) : servers.map(s => (
        <div key={s.name} className="flex-center gap-8"
          style={{ marginBottom: 6, padding: '8px 12px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}>
          <span className="status-dot" style={{ background: s.connected ? '#16a34a' : '#b5b2a8' }}/>
          <span className="fw-600 fs-11" style={{ flexShrink: 0 }}>{s.name}</span>
          <span className="fs-10 text-muted" style={{ flexShrink: 0 }}>{s.transport}</span>
          <span className="fs-10 text-muted mono truncate" style={{ flex: 1 }} title={s.command || s.url}>
            {s.command || s.url}
          </span>
          <span className="fs-10 text-secondary" style={{ flexShrink: 0 }}>
            {s.connected ? `${s.tool_count} 工具` : '未连接'}
          </span>
        </div>
      ))}
    </div>
  )
}
