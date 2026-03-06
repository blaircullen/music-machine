import { useState, useEffect, useRef, useCallback } from 'react'
import { getStats, type StatsResponse } from '../lib/api'

interface UseStatsReturn {
  stats: StatsResponse | null
  loading: boolean
  refetch: () => void
}

export function useStats(): UseStatsReturn {
  const [stats, setStats] = useState<StatsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const mountedRef = useRef(true)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetch = useCallback(() => {
    getStats()
      .then((data) => {
        if (mountedRef.current) {
          setStats(data)
          setLoading(false)
        }
      })
      .catch(() => {
        if (mountedRef.current) setLoading(false)
      })
  }, [])

  useEffect(() => {
    mountedRef.current = true
    fetch()
    timerRef.current = setInterval(fetch, 10_000)

    return () => {
      mountedRef.current = false
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [fetch])

  return { stats, loading, refetch: fetch }
}
