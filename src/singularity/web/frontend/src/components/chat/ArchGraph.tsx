/**
 * 分层 DAG 图 —— 把 `architecture.modules` / `architecture.tasks` 画成盒子 + 箭头。
 *
 * 🔴 **为什么手搓 SVG 而不上 mermaid**（2026-09-20）：`mermaid` 确实躺在 `node_modules`
 * 里，但它是**别人捎进来的传递依赖**（`package.json` 里没有、`src/` 里一次没用过）；
 * 为一个"分层盒子"引入 1MB 的运行时、还要在 React 里管它的异步渲染与清理，不划算。
 * 这里要画的形状只有一种：**依赖关系是 DAG、层内节点互不相干** ⇒ 算层 + 铺格子就够。
 *
 * 数据来源是**架构里本来就有的字段**（模块的 `depends_on`、任务的 `depends_on`），
 * 所以这是**纯渲染**：不新增模型调用、不改调度、不加依赖。
 */
import { memo } from 'react'

export type ArchNode = { id: string; label?: string; deps?: string[] }

/** 盒子尺寸（px）。层与层之间留 `GAP_Y` 画箭头。 */
const BOX_W = 132
const BOX_H = 46
const GAP_X = 18
const GAP_Y = 34
/** 标签按**显示宽度**截断：中日韩字符算 2 格（`len()` 会把中文算成 1，画出来就顶出盒子）。 */
const MAX_LABEL = 16

/** 中日韩统一表意文字起始码位，粗略但够用（`east_asian_width` 那套前端没有）。 */
const isWide = (c: string) => c.charCodeAt(0) > 0x2e80

const dispLen = (s: string) => [...s].reduce((n, c) => n + (isWide(c) ? 2 : 1), 0)

export function truncate(s: string, max = MAX_LABEL): string {
  if (dispLen(s) <= max) return s
  let w = 0
  let out = ''
  for (const c of s) {
    const cw = isWide(c) ? 2 : 1
    if (w + cw > max - 1) break
    w += cw
    out += c
  }
  return out + '…'
}

/**
 * 算每个节点在第几层：**没有依赖的是第 0 层，其余 = 依赖里最深的那个 + 1**。
 *
 * ⚠️ **环**（架构是模型出的，`depends_on` 不保证无环）：按层号迭代 `n` 轮仍定不下来的
 * 节点，**兜底放到最深已知层 + 1**——画歪一点也比**死循环 / 整张图不显示**强。
 * 返回的 `orphans` 就是这批，调用方可以据此提示"依赖有环"。
 */
export function computeLayers(nodes: ArchNode[]): {
  depth: Record<string, number>
  maxDepth: number
  orphans: string[]
} {
  const ids = new Set(nodes.map((n) => n.id))
  const depsOf: Record<string, string[]> = {}
  for (const n of nodes) depsOf[n.id] = (n.deps || []).filter((d) => ids.has(d) && d !== n.id)

  const depth: Record<string, number> = {}
  for (let round = 0; round < nodes.length + 1; round++) {
    let settled = true
    for (const n of nodes) {
      if (depth[n.id] !== undefined) continue
      const ds = depsOf[n.id]
      if (ds.length === 0) {
        depth[n.id] = 0
        continue
      }
      if (ds.every((d) => depth[d] !== undefined)) {
        depth[n.id] = Math.max(...ds.map((d) => depth[d])) + 1
      } else {
        settled = false
      }
    }
    if (settled) break
  }
  const orphans = nodes.filter((n) => depth[n.id] === undefined).map((n) => n.id)
  const known = Object.values(depth)
  const fallback = known.length ? Math.max(...known) + 1 : 0
  for (const id of orphans) depth[id] = fallback

  return { depth, maxDepth: Math.max(0, ...Object.values(depth)), orphans }
}

/** 一层一行的格子。层号决定 y，层内下标决定 x；每层水平居中。 */
export function layout(nodes: ArchNode[]) {
  const { depth, maxDepth, orphans } = computeLayers(nodes)
  const cols: ArchNode[][] = Array.from({ length: maxDepth + 1 }, () => [])
  for (const n of nodes) cols[depth[n.id]].push(n)

  const widest = Math.max(1, ...cols.map((c) => c.length))
  const width = widest * BOX_W + (widest - 1) * GAP_X
  const height = (maxDepth + 1) * BOX_H + maxDepth * GAP_Y

  const pos: Record<string, { x: number; y: number }> = {}
  cols.forEach((col, li) => {
    const rowW = col.length * BOX_W + Math.max(0, col.length - 1) * GAP_X
    const offset = (width - rowW) / 2
    col.forEach((n, ci) => {
      pos[n.id] = { x: offset + ci * (BOX_W + GAP_X), y: li * (BOX_H + GAP_Y) }
    })
  })
  return { pos, width, height, orphans }
}

const edge = (from: { x: number; y: number }, to: { x: number; y: number }) => {
  const x1 = from.x + BOX_W / 2
  const y1 = from.y + BOX_H
  const x2 = to.x + BOX_W / 2
  const y2 = to.y
  const mid = (y1 + y2) / 2
  return `M${x1},${y1} C${x1},${mid} ${x2},${mid} ${x2},${y2}`
}

export const ArchGraph = memo(function ArchGraph({ nodes }: { nodes: ArchNode[] }) {
  if (!nodes.length) return null
  const { pos, width, height, orphans } = layout(nodes)
  const ids = new Set(nodes.map((n) => n.id))
  const edges: { from: string; to: string }[] = []
  for (const n of nodes) for (const d of n.deps || []) if (ids.has(d) && d !== n.id) edges.push({ from: d, to: n.id })

  // ⚠️ **不缩、只滚**：给 svg `width="100%"` 会在窄面板里把整张图等比缩小、字号跟着缩到看不见
  // —— 图是给人看的，宁可横向滚一下。
  return (
    <div style={{ overflowX: 'auto' }}>
      <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
           style={{ display: 'block', fontFamily: 'inherit' }}
           role="img" aria-label="依赖关系图">
        {edges.map((e, i) => (
          <path key={i} d={edge(pos[e.from], pos[e.to])} fill="none" stroke="#c9c5b8" strokeWidth={1.2}
                markerEnd="url(#arch-arrow)" />
        ))}
        <defs>
          <marker id="arch-arrow" viewBox="0 0 8 8" refX={7} refY={4} markerWidth={6} markerHeight={6} orient="auto">
            <path d="M0,0 L8,4 L0,8 z" fill="#c9c5b8" />
          </marker>
        </defs>
        {nodes.map((n) => {
          const p = pos[n.id]
          const twoLine = !!n.label
          return (
            <g key={n.id}>
              {/* 原生 tooltip：完整标题在盒子里放不下，hover 就能看见 —— 白送的 */}
              <title>{n.label ? `${n.id} ${n.label}` : n.id}</title>
              {/* 环里兜底定层的那些标红：图还能看，但"这是模型给的依赖有环"得让人知道 */}
              <rect x={p.x} y={p.y} width={BOX_W} height={BOX_H} rx={6}
                    fill="#faf9f5" stroke={orphans.includes(n.id) ? '#dc2626' : '#e5e2d8'} />
              <text x={p.x + BOX_W / 2} y={p.y + (twoLine ? BOX_H / 2 - 4 : BOX_H / 2 + 4)}
                    textAnchor="middle" fontSize={twoLine ? 10 : 12}
                    fontFamily="var(--font-mono)" fill={twoLine ? '#6b6b68' : '#141413'}>
                {n.id}
              </text>
              {twoLine && (
                <text x={p.x + BOX_W / 2} y={p.y + BOX_H / 2 + 11} textAnchor="middle"
                      fontSize={11} fill="#141413">
                  {truncate(n.label!)}
                </text>
              )}
            </g>
          )
        })}
      </svg>
      {orphans.length > 0 && (
        <div style={{ fontSize: 10, color: '#dc2626', marginTop: 4 }}>
          依赖成环，这几个的位置不准：{orphans.join(', ')}
        </div>
      )}
    </div>
  )
})
