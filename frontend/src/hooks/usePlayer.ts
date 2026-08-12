import { useState, useEffect, useRef, useCallback } from 'react'
import { getStationQueue, postStationFeedback, refreshStation, getStationRefreshStatus, type QueueTrack } from '../lib/api'

export interface PlayerState {
  tracks: QueueTrack[]
  currentIndex: number
  playing: boolean
  currentTime: number
  duration: number
  loading: boolean
  buffering: boolean
  error: string | null
  feedbackSent: Record<number, 'up' | 'down'>
  generatedAt: string | null
}

export interface PlayerControls {
  togglePlay: () => void
  next: () => void
  prev: () => void
  seek: (time: number) => void
  thumbUp: () => void
  thumbDown: () => void
  regenerate: () => void
}

export function usePlayer(stationId: number) {
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const [state, setState] = useState<PlayerState>({
    tracks: [],
    currentIndex: 0,
    playing: false,
    currentTime: 0,
    duration: 0,
    loading: true,
    buffering: false,
    error: null,
    feedbackSent: {},
    generatedAt: null,
  })

  const stateRef = useRef(state)
  stateRef.current = state

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // ------------------------------------------------------------------
  // Load queue on mount
  // ------------------------------------------------------------------

  const loadQueue = useCallback(async (startIndex = 0) => {
    setState(s => ({ ...s, loading: true, error: null }))
    try {
      const queue = await getStationQueue(stationId)
      if (queue.tracks.length === 0) {
        setState(s => ({
          ...s,
          loading: false,
          error: 'No tracks yet. Refresh the station first.',
          tracks: [],
          generatedAt: null,
        }))
        return
      }
      setState(s => ({
        ...s,
        tracks: queue.tracks,
        currentIndex: Math.min(startIndex, queue.tracks.length - 1),
        loading: false,
        generatedAt: queue.generated_at,
        feedbackSent: {},
      }))
    } catch (err) {
      setState(s => ({
        ...s,
        loading: false,
        error: err instanceof Error ? err.message : 'Failed to load queue',
      }))
    }
  }, [stationId])

  useEffect(() => { loadQueue() }, [loadQueue])

  // ------------------------------------------------------------------
  // Audio element setup
  // ------------------------------------------------------------------

  useEffect(() => {
    const audio = new Audio()
    audio.preload = 'auto'
    audioRef.current = audio

    audio.addEventListener('timeupdate', () => {
      setState(s => ({ ...s, currentTime: audio.currentTime }))
    })
    audio.addEventListener('durationchange', () => {
      setState(s => ({ ...s, duration: audio.duration || 0 }))
    })
    audio.addEventListener('playing', () => {
      setState(s => ({ ...s, playing: true, buffering: false }))
    })
    audio.addEventListener('pause', () => {
      setState(s => ({ ...s, playing: false }))
    })
    audio.addEventListener('waiting', () => {
      setState(s => ({ ...s, buffering: true }))
    })
    audio.addEventListener('canplay', () => {
      setState(s => ({ ...s, buffering: false }))
    })
    audio.addEventListener('ended', () => {
      // Auto-advance
      const { currentIndex, tracks } = stateRef.current
      if (currentIndex < tracks.length - 1) {
        setState(s => ({ ...s, currentIndex: s.currentIndex + 1 }))
      } else {
        // Loop back to start
        setState(s => ({ ...s, currentIndex: 0 }))
      }
    })
    audio.addEventListener('error', () => {
      setState(s => ({ ...s, buffering: false, playing: false }))
    })

    return () => {
      audio.pause()
      audio.src = ''
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [])

  // ------------------------------------------------------------------
  // Load track when currentIndex changes
  // ------------------------------------------------------------------

  useEffect(() => {
    const audio = audioRef.current
    if (!audio || state.tracks.length === 0) return

    const track = state.tracks[state.currentIndex]
    const wasPlaying = state.playing

    audio.src = track.stream_url
    audio.load()
    setState(s => ({ ...s, currentTime: 0, duration: 0, buffering: true }))

    if (wasPlaying || state.currentIndex > 0) {
      audio.addEventListener('canplay', function playWhenReady() {
        audio.removeEventListener('canplay', playWhenReady)
        audio.play().catch(() => {
          setState(s => ({ ...s, playing: false, buffering: false }))
        })
      })
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.currentIndex, state.tracks])

  // ------------------------------------------------------------------
  // Controls
  // ------------------------------------------------------------------

  const controls: PlayerControls = {
    togglePlay() {
      const audio = audioRef.current
      if (!audio) return
      if (audio.paused) {
        audio.play().catch(() => setState(s => ({ ...s, playing: false })))
      } else {
        audio.pause()
      }
    },
    next() {
      setState(s => ({
        ...s,
        currentIndex: s.currentIndex < s.tracks.length - 1 ? s.currentIndex + 1 : 0,
      }))
    },
    prev() {
      const audio = audioRef.current
      // If more than 3s in, restart current track; otherwise go back
      if (audio && audio.currentTime > 3) {
        audio.currentTime = 0
      } else {
        setState(s => ({
          ...s,
          currentIndex: s.currentIndex > 0 ? s.currentIndex - 1 : 0,
        }))
      }
    },
    seek(time: number) {
      if (audioRef.current) {
        audioRef.current.currentTime = time
      }
      setState(s => ({ ...s, currentTime: time }))
    },
    async thumbUp() {
      const { tracks, currentIndex, feedbackSent } = stateRef.current
      const track = tracks[currentIndex]
      if (!track || feedbackSent[track.track_id]) return
      setState(s => ({ ...s, feedbackSent: { ...s.feedbackSent, [track.track_id]: 'up' } }))
      try {
        await postStationFeedback(stationId, track.track_id, 'up')
      } catch {
        // Non-critical — don't surface to user
      }
    },
    async thumbDown() {
      const { tracks, currentIndex, feedbackSent } = stateRef.current
      const track = tracks[currentIndex]
      if (!track || feedbackSent[track.track_id]) return
      setState(s => ({ ...s, feedbackSent: { ...s.feedbackSent, [track.track_id]: 'down' } }))
      try {
        await postStationFeedback(stationId, track.track_id, 'down')
      } catch {
        // Non-critical
      }
      // Auto-advance after thumbs down
      controls.next()
    },
    async regenerate() {
      audioRef.current?.pause()
      setState(s => ({ ...s, loading: true, playing: false }))
      try {
        await refreshStation(stationId)
        // Poll until refresh complete
        await new Promise<void>((resolve, reject) => {
          pollRef.current = setInterval(async () => {
            try {
              const status = await getStationRefreshStatus(stationId)
              if (!status.running) {
                clearInterval(pollRef.current!)
                pollRef.current = null
                if (status.error) reject(new Error(status.error))
                else resolve()
              }
            } catch (e) {
              clearInterval(pollRef.current!)
              pollRef.current = null
              reject(e)
            }
          }, 2000)
        })
        await loadQueue()
      } catch (err) {
        setState(s => ({
          ...s,
          loading: false,
          error: err instanceof Error ? err.message : 'Regenerate failed',
        }))
      }
    },
  }

  const currentTrack = state.tracks[state.currentIndex] ?? null

  return { state, controls, currentTrack }
}
