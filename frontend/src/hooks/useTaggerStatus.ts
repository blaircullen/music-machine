import { useState, useEffect, useRef, useCallback } from 'react'

export interface TaggerStatus {
  running: boolean
  phase: 'idle' | 'scanning' | 'tagging' | 'complete' | 'failed'
  processed: number
  total: number
  tagged: number
  failed: number
  skipped: number
  current_file: string | null
  elapsed_s: number
}

const INITIAL: TaggerStatus = {
  running: false,
  phase: 'idle',
  processed: 0,
  total: 0,
  tagged: 0,
  failed: 0,
  skipped: 0,
  current_file: null,
  elapsed_s: 0,
}

export function useTaggerStatus() {
  const [status, setStatus] = useState<TaggerStatus>(INITIAL)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const mountedRef = useRef(true)

  const poll = useCallback(() => {
    fetch('/api/tagger/status')
      .then(r => r.json())
      .then((data: TaggerStatus) => {
        if (mountedRef.current) setStatus(data)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    mountedRef.current = true
    poll()
    timerRef.current = setInterval(poll, 2000)

    return () => {
      mountedRef.current = false
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [poll])

  return { status, poll }
}
