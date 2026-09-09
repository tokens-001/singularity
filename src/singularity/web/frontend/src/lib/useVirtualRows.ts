import { useEffect, useRef, useState, type UIEvent } from 'react'

/**
 * 定高行的极简窗口化：只渲染可视区 + overscan 条，上下用占位块撑开滚动条。
 * 用于 Tasks 这类「任务上千条」的长列表 —— 全量渲染时每次 SSE 刷新都要 diff 上千个元素。
 * 前提：每行等高（Tasks 的行是单行截断文本，宽度变化也不换行）。
 */
export function useVirtualRows(total: number, rowHeight: number, gap = 0, overscan = 8) {
  const ref = useRef<HTMLDivElement>(null)
  const [viewH, setViewH] = useState(600)
  const [scrollTop, setScrollTop] = useState(0)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(() => setViewH(el.clientHeight))
    ro.observe(el)
    setViewH(el.clientHeight)
    return () => ro.disconnect()
  }, [])

  const step = rowHeight + gap
  const perScreen = Math.max(1, Math.ceil(viewH / step))
  const start = Math.max(0, Math.floor(scrollTop / step) - overscan)
  const end = Math.min(total, start + perScreen + overscan * 2)

  return {
    ref,
    onScroll: (e: UIEvent<HTMLDivElement>) => setScrollTop(e.currentTarget.scrollTop),
    start,
    end,
    padTop: start * step,
    padBottom: Math.max(0, (total - end) * step),
  }
}
