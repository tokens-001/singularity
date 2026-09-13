/**
 * McpTab.tsx —— 样板重写（原件：src/components/McpTab.tsx，65 行）。
 *
 * **原版踩了哪颗雷，这版怎么排的**：
 *
 * 1. 原版加载失败只 toast 一声，`servers` 停在初始 `[]` —— 界面渲染的是
 *    空态文案「没有 MCP 服务器」：**把"读不到"说成了"没有"**（GATE3 假绿灯
 *    的静默孪生兄弟，只是这次默认侧是灰不是绿）。这版四态走 AsyncBoundary，
 *    失败有自己的相、自己的文字、自己的重试入口。
 * 2. 原版 `{s.connected ? 绿点 : 灰点}` —— connected 缺失时渲染灰点「未连接」，
 *    "不知道"被说成了"没连上"。这版缺失 → 琥珀「状态未知」。
 * 3. 原版把 `tool_count` 直接拼进模板串，缺失时渲染「undefined 工具」；
 *    且"未连接"时 tool_count 本来就是 0，模板串会渲染「0 工具」——
 *    **0 只有真数过才许显示**，这版数字缺失 → 「工具数未知」。
 * 4. 后端明明给了 `enabled` 字段（_api_admin.py:391），原版没显示 ——
 *    主动停用的服务器和连不上的服务器在界面上长得一样。这版「已停用」单独说。
 *
 * 判据细节（每个字段缺失/空串/不认识时显示什么）见本目录 README.md 的字段表。
 *
 * 提升路径：评审通过后，本文件内容原样覆盖 src/components/McpTab.tsx，
 * 相对路径 `../../lib/…` 改成 `../lib/…`、`./Async` 不变（Async.tsx 同批提升到
 * src/components/）。Config.tsx 不用动 —— 默认导出名和签名没变。
 */
import { useEffect } from 'react'
import { api } from '../../lib/api'
import { errText, useToast } from '../../lib/toast'
import { RefreshCw } from 'lucide-react'
import { AsyncBoundary, useResource, type Loadable } from './Async'

/**
 * 后端契约（_api_admin.py `mcp_server_list`）：8 个键**恒给**，但值可能是空串
 * （name/command/url 来自手编的 mcp_servers.toml，`load_mcp_configs` 对缺键取 ""）。
 * 这里把「契约恒给」的键仍标成可选：渲染判据必须对 undefined 有话说 ——
 * 形状漂移要落到**文字**上，不许静默落到某个"看着还行"的默认值。
 */
interface McpServer {
  name: string
  transport: string
  command: string
  url: string
  enabled?: boolean
  connected?: boolean
  tool_count?: number
}

/** /api/mcp/tools 的条目。本页只用计数，字段放宽。 */
type McpTool = { name: string }

interface McpData {
  servers: McpServer[]
  tools: McpTool[]
}

/** 连接状态 = 圆点 + 文字，一个事实一个组件 —— 拆成两处各判一遍迟早漂。
 *  顺序即优先级：矛盾 > 停用 > 未知 > 连接状态。
 *  配色口径：绿=已连接 · 灰=停用/未连接（没有数据或没有这回事）· 琥珀=核不了。 */
function ConnStatus({ server: s }: { server: McpServer }) {
  if (s.enabled === false && s.connected) {
    return (
      <Status bg="#b45309" text="配置矛盾" tone="amber"
        title="配置里 enabled=false，但注册表里它是连接状态 —— 两处数据打架，先别信" />
    )
  }
  if (s.enabled === false) {
    return <Status bg="#b5b2a8" text="已停用" title="mcp.toml 里 enabled=false —— 是停用，不是故障" />
  }
  if (s.connected === undefined) {
    return (
      <Status bg="#b45309" text="状态未知" tone="amber"
        title="后端响应里没有 connected 字段 —— 不知道它连没连上" />
    )
  }
  if (s.connected) {
    // 「0 工具」只有真数过才许显示；数字缺失就是未知，别拿 0 充数（同 money.ts 的纪律）
    const text =
      typeof s.tool_count === 'number' ? `${s.tool_count} 工具` : '已连接 · 工具数未知'
    return <Status bg="#16a34a" text={text} />
  }
  return <Status bg="#b5b2a8" text="未连接" />
}

function Status({
  bg,
  text,
  tone,
  title,
}: {
  bg: string
  text: string
  /** amber = 这个状态本身需要人注意（未知/矛盾），给警示色文字 */
  tone?: 'amber'
  title?: string
}) {
  return (
    <span className="flex-center gap-6" style={{ flexShrink: 0 }} title={title}>
      <span className="status-dot" style={{ background: bg }} aria-hidden />
      <span className="fs-10" style={{ color: tone === 'amber' ? '#b45309' : 'var(--text-secondary, #6b6b68)' }}>
        {text}
      </span>
    </span>
  )
}

/** 单行。name / transport / command+url 各自的缺失判据见 README 字段表。 */
function ServerRow({ server: s }: { server: McpServer }) {
  const where = s.command || s.url // stdio 给 command，http 给 url；正常情况下恰有一个非空
  return (
    <div
      className="flex-center gap-8"
      style={{ marginBottom: 6, padding: '8px 12px', background: 'var(--bg-secondary)', borderRadius: 'var(--radius)' }}
    >
      <ConnStatus server={s} />
      {/* name 为空串是真的可能发生的（toml 条目漏写 name）—— 显示出来，别渲染成看不见的空行；
          React key 同理：空串 key 会让两条未命名服务器撞 key */}
      <span className="fw-600 fs-11" style={{ flexShrink: 0 }}>
        {s.name || '（未命名）'}
      </span>
      {/* transport 是后端不校验取值的自由词 —— 生词原样回显。不认识的枚举值 ≠ 空 */}
      <span className="fs-10 text-muted" style={{ flexShrink: 0 }}>
        {s.transport}
      </span>
      <span className="fs-10 text-muted mono truncate" style={{ flex: 1 }} title={where}>
        {where || <span className="text-muted">（未配置启动方式 —— stdio 要 command，http 要 url）</span>}
      </span>
    </div>
  )
}

export default function McpTab() {
  const toast = useToast()
  const { state, reloading, load } = useResource<McpData>(async () => {
    const [servers, tools] = await Promise.all([api.mcpServers(), api.mcpTools()])
    return { servers: servers as McpServer[], tools: tools as McpTool[] }
  }, { errorLabel: '加载 MCP 服务器失败' })

  useEffect(() => { void load(true) }, [load])

  /** 重载 mcp.toml。后端回 {ok, servers, tools} —— 计数用后端给的，别自己编。
   *  刷新失败走 toast（写操作的既有纪律），随后的列表重拉若也失败会以
   *  staleError/错误态现身，不靠 toast 记事。 */
  const reloadConfig = async () => {
    try {
      const r: any = await api.mcpRefresh()
      const ok = r?.ok !== false
      toast(`已重载：${r?.servers ?? '?'} 个服务器 · ${r?.tools ?? '?'} 个工具`, ok ? 'success' : 'info')
    } catch (e) {
      toast(errText(e, '重载失败'), 'error')
    }
    void load(false)
  }

  const data = state.phase === 'ready' ? state.data : null

  // 本页的资源是 {servers, tools} 两个列表，但四态判据跟着**主列表** servers 走
  // （tools 只是头部的计数）。AsyncBoundary 的空判据是 `length === 0`，
  // 所以把 Loadable 切到 servers 视角再交给它 —— 相（loading/error/staleError）原样保留。
  const listState: Loadable<McpServer[]> =
    state.phase === 'ready' ? { ...state, data: state.data.servers } : state

  return (
    <div>
      <div className="flex-center gap-8" style={{ marginBottom: 8 }}>
        {/* 计数只在真拿到数据时显示 —— 加载中/失败时显示 (0) 是编的 */}
        <span className="fw-600 fs-12 text-secondary">
          MCP 服务器{data ? `（${data.servers.length}）` : ''}
        </span>
        {data && data.servers.length > 0 && data.tools.length === 0 && (
          <span className="fs-10" style={{ color: '#b45309' }}>
            0 个工具 —— 这些服务器没有一个连接成功（或连上了但没提供工具）
          </span>
        )}
        {data && data.tools.length > 0 && (
          <span className="fs-10 text-muted">{data.tools.length} 个工具</span>
        )}
        <span className="flex-1" />
        <button
          onClick={() => void reloadConfig()}
          disabled={reloading}
          className="btn-sm"
          title="重新读取 mcp.toml 并装载，不用重启后端"
        >
          <RefreshCw size={12} /> {reloading ? '重载中…' : '重新加载配置'}
        </button>
      </div>

      <AsyncBoundary
        state={listState}
        loadingText="正在加载 MCP 服务器…"
        emptyText="没有配置任何 MCP 服务器。"
        emptyHint="编辑 mcp.toml 后点「重新加载配置」即可生效，无需重启后端。"
        onRetry={() => void load(true)}
      >
        {(d) => (
          <>
            {d.map((s, i) => (
              <ServerRow key={s.name || `__unnamed_${i}`} server={s} />
            ))}
          </>
        )}
      </AsyncBoundary>
    </div>
  )
}
