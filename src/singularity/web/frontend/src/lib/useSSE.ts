import { useEffect, useRef, useCallback } from 'react'

type SSECallback = (event: { kind: string; [key: string]: any }) => void

export function useSSE(onEvent: SSECallback) {
  const onEventRef = useRef(onEvent)
  onEventRef.current = onEvent

  useEffect(() => {
    const es = new EventSource('/api/events')
    
    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data)
        onEventRef.current(data)
      } catch {}
    }

    es.onerror = () => {
      // EventSource auto-reconnects
    }

    return () => es.close()
  }, [])
}
