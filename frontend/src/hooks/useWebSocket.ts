import { useEffect, useRef, useState, useCallback } from 'react'

export interface WebSocketMessage {
  type: 'scan_progress' | 'job_update' | 'stats_update'
  data: Record<string, unknown>
}

interface UseWebSocketReturn {
  lastMessage: WebSocketMessage | null
  isConnected: boolean
}

const MIN_DELAY = 1000
const MAX_DELAY = 30000

function getWsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws`
}

export function useWebSocket(): UseWebSocketReturn {
  const [lastMessage, setLastMessage] = useState<WebSocketMessage | null>(null)
  const [isConnected, setIsConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const delayRef = useRef(MIN_DELAY)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mountedRef = useRef(true)

  const connect = useCallback(() => {
    if (!mountedRef.current) return

    try {
      const ws = new WebSocket(getWsUrl())
      wsRef.current = ws

      ws.onopen = () => {
        if (!mountedRef.current) { ws.close(); return }
        setIsConnected(true)
        delayRef.current = MIN_DELAY
      }

      ws.onmessage = (event: MessageEvent<string>) => {
        if (!mountedRef.current) return
        try {
          const msg = JSON.parse(event.data) as WebSocketMessage
          setLastMessage(msg)
        } catch {
          // ignore malformed frames
        }
      }

      ws.onclose = () => {
        if (!mountedRef.current) return
        setIsConnected(false)
        wsRef.current = null
        const delay = delayRef.current
        delayRef.current = Math.min(delay * 2, MAX_DELAY)
        timerRef.current = setTimeout(connect, delay)
      }

      ws.onerror = () => {
        ws.close()
      }
    } catch {
      if (!mountedRef.current) return
      const delay = delayRef.current
      delayRef.current = Math.min(delay * 2, MAX_DELAY)
      timerRef.current = setTimeout(connect, delay)
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    connect()

    return () => {
      mountedRef.current = false
      if (timerRef.current) clearTimeout(timerRef.current)
      if (wsRef.current) {
        wsRef.current.onclose = null
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [connect])

  return { lastMessage, isConnected }
}
