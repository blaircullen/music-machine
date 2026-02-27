import { useState, useEffect, useRef } from 'react'

export interface ReorgLastRun {
  timestamp: string
  elapsed_s: number
  total: number
  moved: number
  skipped: number
  errors: number
  already_ok: number
  inbox_moved: number
  inbox_skipped: number
  error?: string
}

export interface ReorgStatus {
  running: boolean
  phase: string
  total: number
  progress: number
  current_file: string
  elapsed_s: number
  moved: number
  skipped: number
  errors: number
  already_ok: number
  inbox_moved: number
  last_run: ReorgLastRun | null
}

const INITIAL: ReorgStatus = {
  running: false,
  phase: 'idle',
  total: 0,
  progress: 0,
  current_file: '',
  elapsed_s: 0,
  moved: 0,
  skipped: 0,
  errors: 0,
  already_ok: 0,
  inbox_moved: 0,
  last_run: null,
}

export function useReorgStatus() {
  const [status, setStatus] = useState<ReorgStatus>(INITIAL)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    const poll = () => {
      fetch('/api/reorg/status')
        .then((r) => r.json())
        .then((data: ReorgStatus) => setStatus(data))
        .catch(() => {})
    }
    poll()
    timerRef.current = setInterval(poll, 3000)
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [])

  return status
}
