import { useState, useEffect, useRef, useCallback } from 'react'

export interface UpgradeStatus {
  running: boolean
  phase: 'idle' | 'searching' | 'downloading' | 'complete' | 'failed'
  searched: number
  found: number
  downloading: number
  completed: number
  failed: number
  // Per-item download detail
  current_track: string | null
  current_artist: string | null
  current_title: string | null
  current_album: string | null
  current_step: 'downloading' | 'importing' | null
  current_bytes: number
  current_total_bytes: number
  download_index: number
  download_total: number
}

const INITIAL: UpgradeStatus = {
  running: false,
  phase: 'idle',
  searched: 0,
  found: 0,
  downloading: 0,
  completed: 0,
  failed: 0,
  current_track: null,
  current_artist: null,
  current_title: null,
  current_album: null,
  current_step: null,
  current_bytes: 0,
  current_total_bytes: 0,
  download_index: 0,
  download_total: 0,
}

export function useUpgradeStatus() {
  const [status, setStatus] = useState<UpgradeStatus>(INITIAL)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const mountedRef = useRef(true)

  const poll = useCallback(() => {
    fetch('/api/upgrades/status')
      .then(r => r.json())
      .then((data: UpgradeStatus) => {
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
