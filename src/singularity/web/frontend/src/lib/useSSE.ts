import { useEffect, useRef, useState } from 'react'

type SSECallback = (event: { kind: string; [key: string]: any }) => void

interface SSEOptions {
  /** 只关心这些 kind；不传 = 全部 */
  kinds?: string[]
  /** 合并连发事件，只处理最后一次（毫秒）。任务执行时 tool:* 事件很密 */
  debounceMs?: number
}

// 全局单例：所有页面共享一条 SSE 连接，避免每页各连一条撞 _MAX_SSE_CLIENTS(20)。
// 之前 Chat/Tasks/Projects 各 new 一条，多标签页下易超限被 503 拒(观察者转圈那次)。
let _es: EventSource | null = null
const _subscribers = new Set<(e: any) => void>()

// 连接状态：SSE 活着就别再轮询（轮询只当断线兜底）
let _connected = false
const _statusSubs = new Set<(c: boolean) => void>()
function _setConnected(v: boolean) {
  if (_connected === v) return
  _connected = v
  _statusSubs.forEach((f) => f(v))
}

function _ensureEs(): EventSource {
  if (_es) return _es
  _es = new EventSource('/api/events')
  _es.onopen = () => _setConnected(true)
  _es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data)
      _subscribers.forEach((cb) => cb(data))
    } catch {}
  }
  _es.onerror = () => {
    _setConnected(false)
    // EventSource auto-reconnects
  }
  return _es
}

/** SSE 当前是否连着。用来决定要不要开轮询兜底。 */
export function useSSEConnected(): boolean {
  const [ok, setOk] = useState(_connected)
  useEffect(() => {
    _statusSubs.add(setOk)
    setOk(_connected)
    return () => { _statusSubs.delete(setOk) }
  }, [])
  return ok
}

export function useSSE(onEvent: SSECallback, opts?: SSEOptions) {
  const onEventRef = useRef(onEvent)
  onEventRef.current = onEvent
  const kindsKey = opts?.kinds?.join(',') || ''
  const debounceMs = opts?.debounceMs || 0

  useEffect(() => {
    _ensureEs()
    const kindSet = kindsKey ? new Set(kindsKey.split(',')) : null
    let timer: ReturnType<typeof setTimeout> | undefined
    const cb = (data: any) => {
      if (kindSet && !kindSet.has(data?.kind)) return
      if (!debounceMs) { onEventRef.current(data); return }
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => onEventRef.current(data), debounceMs)
    }
    _subscribers.add(cb)
    return () => {
      _subscribers.delete(cb)
      if (timer) clearTimeout(timer)
      // ponytail: 单例保持存活，不随最后订阅者退出而 close——
      // 页面切换(卸载/挂载)时避免频繁重连，连接数恒定 1 条/标签页。
    }
  }, [kindsKey, debounceMs])
}
