import { useEffect, useRef } from 'react'

type SSECallback = (event: { kind: string; [key: string]: any }) => void

// 全局单例：所有页面共享一条 SSE 连接，避免每页各连一条撞 _MAX_SSE_CLIENTS(20)。
// 之前 Chat/Tasks/Projects 各 new 一条，多标签页下易超限被 503 拒(观察者转圈那次)。
let _es: EventSource | null = null
const _subscribers = new Set<(e: any) => void>()

function _ensureEs(): EventSource {
  if (_es) return _es
  _es = new EventSource('/api/events')
  _es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data)
      _subscribers.forEach((cb) => cb(data))
    } catch {}
  }
  _es.onerror = () => {
    // EventSource auto-reconnects
  }
  return _es
}

export function useSSE(onEvent: SSECallback) {
  const onEventRef = useRef(onEvent)
  onEventRef.current = onEvent

  useEffect(() => {
    _ensureEs()
    const cb = (data: any) => onEventRef.current(data)
    _subscribers.add(cb)
    return () => {
      _subscribers.delete(cb)
      // ponytail: 单例保持存活，不随最后订阅者退出而 close——
      // 页面切换(卸载/挂载)时避免频繁重连，连接数恒定 1 条/标签页。
    }
  }, [])
}
