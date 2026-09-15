import { Component, type ReactNode } from 'react'

interface Props { children: ReactNode }
interface State { error: Error | null }

/**
 * 「页面手里的 chunk 已经不存在了」这一类错误。
 *
 * **什么时候会发生**：`npm run build` 之后，懒加载路由的 chunk 名带上了**新的内容哈希**，
 * 文件名整个换掉。**已经打开的旧页面**还攥着旧名字，一点那个路由就 404
 * ⇒ 浏览器抛这个错，被 ErrorBoundary 显示成"页面出错了"。
 *
 * ⚠️ **"重试"救不了它** —— 那个文件是真没了，重试多少次都是同一个 404。
 * 只有**刷新**（重新拿 index.html、拿到新的 chunk 名）能解。
 * 2026-09-15 真机撞上：用户点「项目」页 → "页面出错了 / Importing a module script
 * failed." + 一个**永远好不了**的「重试」。**根因是我们自己 rebuild 了前端。**
 *
 * ⚠️ 下面这串**只用来决定"哪个按钮排在前面"**，不是判据的命门 ——
 * **刷新按钮无论认不认得出都照常提供**（认不出时排在"重试"后面）。
 * 所以这张表不全会导致"认不出"，**不会导致"没有出路"**。
 */
const STALE_CHUNK_HINTS = [
  'Importing a module script failed',            // Chromium
  'Failed to fetch dynamically imported module', // Firefox / Chromium 另一路
  'error loading dynamically imported module',   // Safari
  'ChunkLoadError',                              // webpack 系（本仓不用，同族先收着）
]

function isStaleChunk(e: Error | null): boolean {
  const s = `${e?.name ?? ''} ${e?.message ?? ''}`
  return STALE_CHUNK_HINTS.some(h => s.includes(h))
}

const btnStyle = (primary: boolean) => ({
  padding: '6px 16px', borderRadius: 'var(--radius)', cursor: 'pointer',
  border: primary ? 'none' : '1px solid var(--border)',
  background: primary ? 'var(--accent)' : 'transparent',
  color: primary ? '#fff' : 'var(--text-muted)',
} as const)

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    if (!this.state.error) return this.props.children
    const stale = isStaleChunk(this.state.error)
    const reload = (
      <button key="reload" onClick={() => window.location.reload()} style={btnStyle(stale)}>
        刷新页面
      </button>
    )
    const retry = (
      <button key="retry" onClick={() => this.setState({ error: null })} style={btnStyle(!stale)}>
        重试
      </button>
    )
    return (
      <div style={{ padding: 40, textAlign: 'center' }}>
        <h2 style={{ color: 'var(--accent-red)' }}>
          {stale ? '页面版本过期了' : '页面出错了'}
        </h2>
        {stale && (
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 10 }}>
            多半是应用刚发布了新版本，这个页面手里的文件已经不存在了 —— <b>刷新一下就好</b>。
          </div>
        )}
        <pre style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 10, whiteSpace: 'pre-wrap' }}>
          {this.state.error.message}
        </pre>
        <div style={{ display: 'flex', gap: 10, justifyContent: 'center', marginTop: 14 }}>
          {stale ? [reload, retry] : [retry, reload]}
        </div>
      </div>
    )
  }
}
