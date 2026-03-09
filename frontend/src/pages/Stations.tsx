import { useState, useEffect, useCallback, useRef } from 'react'
import { Radio, Plus, RefreshCw, Trash2, Check, X, Headphones, Music2 } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { useNavigate } from 'react-router-dom'
import { GlassCard } from '../components/ui/GlassCard'
import { Button } from '../components/ui/Button'
import { Modal } from '../components/ui/Modal'
import { EmptyState } from '../components/ui/EmptyState'
import {
  getStations, createStation, deleteStation,
  refreshStation, getStationRefreshStatus, searchStationTracks,
  getAnalysisStats,
  type Station, type StationCreate, type SeedTrack, type AnalysisStats,
} from '../lib/api'
import toast from 'react-hot-toast'

// ------------------------------------------------------------------
// Equalizer bars
// ------------------------------------------------------------------

function EqualizerBars({ active }: { active: boolean }) {
  const heights = [0.45, 0.80, 1.0, 0.65, 0.85]
  return (
    <div className="flex items-end gap-[2px] h-3.5 shrink-0">
      {heights.map((h, i) => (
        <motion.div
          key={i}
          className="w-[3px] rounded-full"
          style={{ backgroundColor: active ? '#d4a017' : 'rgba(212,160,23,0.35)' }}
          animate={{
            height: [
              `${h * (active ? 14 : 7)}px`,
              `${(1.05 - h) * (active ? 14 : 7) + 1}px`,
              `${h * (active ? 14 : 7)}px`,
            ],
          }}
          transition={{
            duration: active ? 0.45 : 1.8,
            repeat: Infinity,
            delay: i * (active ? 0.07 : 0.22),
            ease: 'easeInOut',
          }}
        />
      ))}
    </div>
  )
}

// ------------------------------------------------------------------
// Refresh flavor text (sonic edition)
// ------------------------------------------------------------------

const REFRESH_MSGS = [
  'Computing seed centroid…',
  'Scanning feature matrix…',
  'Running cosine similarity…',
  'Applying preference weights…',
  'Drawing weighted sample…',
  'Syncing playlist to Plex…',
]

function useRefreshMessages(active: boolean) {
  const [idx, setIdx] = useState(0)
  useEffect(() => {
    if (!active) { setIdx(0); return }
    const id = setInterval(() => setIdx(i => (i + 1) % REFRESH_MSGS.length), 2600)
    return () => clearInterval(id)
  }, [active])
  return active ? REFRESH_MSGS[idx] : null
}

// ------------------------------------------------------------------
// Track search autocomplete
// ------------------------------------------------------------------

function formatDuration(secs: number | null): string {
  if (!secs) return ''
  const m = Math.floor(secs / 60)
  const s = Math.floor(secs % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

interface TrackSearchProps {
  onSelect: (track: SeedTrack) => void
  disabled?: boolean
}

function TrackSearch({ onSelect, disabled }: TrackSearchProps) {
  const [q, setQ] = useState('')
  const [results, setResults] = useState<SeedTrack[]>([])
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const wrapperRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    if (q.length < 2) { setResults([]); setOpen(false); return }
    debounceRef.current = setTimeout(async () => {
      setLoading(true)
      try {
        const res = await searchStationTracks(q)
        setResults(res)
        setOpen(res.length > 0)
      } catch {
        setResults([])
      } finally {
        setLoading(false)
      }
    }, 300)
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current) }
  }, [q])

  // Close on outside click
  useEffect(() => {
    function handle(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handle)
    return () => document.removeEventListener('mousedown', handle)
  }, [])

  function select(track: SeedTrack) {
    onSelect(track)
    setQ('')
    setResults([])
    setOpen(false)
  }

  return (
    <div ref={wrapperRef} className="relative">
      <div className="flex items-center gap-2 bg-[#1a1d27] border border-[#2a2d3a] rounded-lg px-3 py-2 focus-within:border-[#d4a017]/50">
        <Music2 className="w-3.5 h-3.5 text-slate-500 shrink-0" />
        <input
          type="text"
          value={q}
          onChange={e => setQ(e.target.value)}
          onFocus={() => results.length > 0 && setOpen(true)}
          placeholder="Search artist, title, or album…"
          disabled={disabled}
          className="flex-1 bg-transparent text-sm text-white placeholder-slate-600 focus:outline-none disabled:opacity-40"
        />
        {loading && (
          <div className="w-3.5 h-3.5 border border-[#d4a017] border-t-transparent rounded-full animate-spin shrink-0" />
        )}
      </div>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.15 }}
            className="absolute z-50 w-full mt-1 bg-[#1e2130] border border-[#2a2d3a] rounded-lg shadow-xl overflow-hidden max-h-64 overflow-y-auto"
          >
            {results.map(track => (
              <button
                key={track.id}
                onClick={() => select(track)}
                className="w-full flex items-center gap-3 px-3 py-2.5 hover:bg-[#2a2d3a] transition-colors text-left"
              >
                <div className="min-w-0 flex-1">
                  <div className="text-sm text-white truncate">{track.title}</div>
                  <div className="text-[11px] text-slate-400 truncate">
                    {track.artist}{track.album ? ` · ${track.album}` : ''}
                  </div>
                </div>
                {track.duration && (
                  <span className="text-[11px] text-slate-500 shrink-0">
                    {formatDuration(track.duration)}
                  </span>
                )}
              </button>
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ------------------------------------------------------------------
// Create Station Modal
// ------------------------------------------------------------------

interface CreateModalProps {
  open: boolean
  onClose: () => void
  onCreated: (station: Station) => void
}

function CreateStationModal({ open, onClose, onCreated }: CreateModalProps) {
  const [name, setName] = useState('')
  const [seedTracks, setSeedTracks] = useState<SeedTrack[]>([])
  const [saving, setSaving] = useState(false)

  function reset() {
    setName('')
    setSeedTracks([])
    setSaving(false)
  }

  function handleClose() { reset(); onClose() }

  function addTrack(track: SeedTrack) {
    if (!seedTracks.some(t => t.id === track.id)) {
      setSeedTracks(prev => [...prev, track])
    }
  }

  function removeTrack(id: number) {
    setSeedTracks(prev => prev.filter(t => t.id !== id))
  }

  async function handleSave() {
    if (!name.trim()) { toast.error('Station name required'); return }
    if (seedTracks.length === 0) { toast.error('Add at least one seed track'); return }
    if (seedTracks.length > 5) { toast.error('Maximum 5 seed tracks'); return }

    const data: StationCreate = {
      name: name.trim(),
      seed_track_ids: seedTracks.map(t => t.id),
    }

    setSaving(true)
    try {
      const station = await createStation(data)
      onCreated(station)
      toast.success(`Station "${station.name}" created`)
      handleClose()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to create station')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal open={open} title="New Station" onClose={handleClose}>
      <div className="space-y-5 p-1">
        {/* Name */}
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Station Name</label>
          <input
            type="text"
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder="e.g. Morning Ride"
            className="w-full bg-[#1a1d27] border border-[#2a2d3a] rounded-lg px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-[#d4a017]/50"
          />
        </div>

        {/* Seed Tracks */}
        <div>
          <label className="text-xs text-slate-400 mb-1 block">
            Seed Tracks <span className="text-slate-600">(3–5 tracks that define the vibe)</span>
          </label>
          <TrackSearch onSelect={addTrack} disabled={seedTracks.length >= 5} />
          {seedTracks.length > 0 && (
            <div className="mt-2 space-y-1.5">
              {seedTracks.map(track => (
                <div
                  key={track.id}
                  className="flex items-center gap-2 bg-[#d4a017]/10 rounded-lg px-3 py-2"
                >
                  <div className="min-w-0 flex-1">
                    <div className="text-sm text-[#f0c95c] truncate">{track.title}</div>
                    <div className="text-[11px] text-slate-400 truncate">{track.artist}</div>
                  </div>
                  <button
                    onClick={() => removeTrack(track.id)}
                    className="text-slate-500 hover:text-white transition-colors shrink-0"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}
          {seedTracks.length === 0 && (
            <p className="text-[11px] text-slate-600 mt-1.5">
              Search for tracks from your library above. The station will find similar-sounding music using acoustic similarity.
            </p>
          )}
        </div>

        {/* Actions */}
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="secondary" onClick={handleClose}>Cancel</Button>
          <Button onClick={handleSave} disabled={saving}>
            {saving ? 'Creating...' : 'Create Station'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}

// ------------------------------------------------------------------
// Station Card
// ------------------------------------------------------------------

interface StationCardProps {
  station: Station
  index: number
  onDelete: (id: number) => void
  onRefreshed: (station: Station) => void
}

function StationCard({ station, index, onDelete, onRefreshed }: StationCardProps) {
  const navigate = useNavigate()
  const [refreshing, setRefreshing] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [justRefreshed, setJustRefreshed] = useState(false)
  const refreshMsg = useRefreshMessages(refreshing)

  async function handleRefresh() {
    setRefreshing(true)
    try {
      const result = await refreshStation(station.id)
      if (!result.ok) {
        toast.error(result.error ?? 'Refresh failed')
        setRefreshing(false)
        return
      }
      const poll = setInterval(async () => {
        try {
          const status = await getStationRefreshStatus(station.id)
          if (!status.running) {
            clearInterval(poll)
            setRefreshing(false)
            if (status.error) {
              toast.error(status.error)
            } else {
              toast.success(`"${station.name}" refreshed`)
              setJustRefreshed(true)
              setTimeout(() => setJustRefreshed(false), 1000)
              const stations = await getStations()
              const updated = stations.find(s => s.id === station.id)
              if (updated) onRefreshed(updated)
            }
          }
        } catch {
          clearInterval(poll)
          setRefreshing(false)
        }
      }, 2000)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Refresh failed')
      setRefreshing(false)
    }
  }

  async function handleDelete() {
    try {
      await deleteStation(station.id)
      onDelete(station.id)
      toast.success(`Station "${station.name}" deleted`)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Delete failed')
    }
  }

  const lastRefreshed = station.last_refreshed
    ? new Date(station.last_refreshed).toLocaleDateString('en-US', {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      })
    : 'Never refreshed'

  return (
    <motion.div
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, delay: index * 0.07, ease: [0.25, 1, 0.5, 1] }}
    >
      <GlassCard className={`p-5 transition-all duration-500 ${justRefreshed ? 'ring-1 ring-[#d4a017]/40' : ''}`}>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 mb-3">
              <EqualizerBars active={refreshing} />
              <h3 className="text-white font-semibold text-sm truncate font-[family-name:var(--font-family-display)]">
                {station.name}
              </h3>
            </div>

            <div className="text-[10px] text-slate-500 space-y-0.5 min-h-[2.5rem]">
              <AnimatePresence mode="wait">
                {refreshing && refreshMsg ? (
                  <motion.div
                    key={refreshMsg}
                    initial={{ opacity: 0, y: 3 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -3 }}
                    transition={{ duration: 0.25 }}
                    className="text-[#d4a017]/70 italic"
                  >
                    {refreshMsg}
                  </motion.div>
                ) : (
                  <motion.div
                    key="stats"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ duration: 0.2 }}
                  >
                    <div>
                      <span className="text-slate-400">{station.track_count}</span> tracks
                      {' · '}
                      <span className="text-slate-400">{station.plex_playlist_name}</span>
                    </div>
                    <div>{lastRefreshed}</div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </div>

          <div className="flex flex-col gap-1.5 shrink-0">
            {/* Listen button */}
            {station.track_count > 0 && (
              <button
                onClick={() => navigate(`/listen/${station.id}`)}
                title="Open player"
                className="p-1.5 text-slate-500 hover:text-[#d4a017] transition-colors"
              >
                <Headphones className="w-4 h-4" />
              </button>
            )}
            <button
              onClick={handleRefresh}
              disabled={refreshing}
              title="Refresh now"
              className="p-1.5 text-slate-500 hover:text-[#d4a017] transition-colors disabled:opacity-40"
            >
              <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
            </button>
            {confirmDelete ? (
              <div className="flex gap-1">
                <button onClick={handleDelete} title="Confirm delete"
                  className="p-1 text-red-400 hover:text-red-300 transition-colors">
                  <Check className="w-4 h-4" />
                </button>
                <button onClick={() => setConfirmDelete(false)} title="Cancel"
                  className="p-1 text-slate-500 hover:text-slate-300 transition-colors">
                  <X className="w-4 h-4" />
                </button>
              </div>
            ) : (
              <button onClick={() => setConfirmDelete(true)} title="Delete station"
                className="p-1.5 text-slate-500 hover:text-red-400 transition-colors">
                <Trash2 className="w-4 h-4" />
              </button>
            )}
          </div>
        </div>
      </GlassCard>
    </motion.div>
  )
}

// ------------------------------------------------------------------
// Stations page
// ------------------------------------------------------------------

export default function Stations() {
  const [stations, setStations] = useState<Station[]>([])
  const [loading, setLoading] = useState(true)
  const [createOpen, setCreateOpen] = useState(false)
  const [stats, setStats] = useState<AnalysisStats | null>(null)

  const load = useCallback(async () => {
    try {
      const [stationsData, statsData] = await Promise.all([
        getStations(),
        getAnalysisStats().catch(() => null),
      ])
      setStations(stationsData)
      setStats(statsData)
    } catch {
      toast.error('Failed to load stations')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  function handleCreated(station: Station) {
    setStations(prev => [station, ...prev])
  }

  function handleDelete(id: number) {
    setStations(prev => prev.filter(s => s.id !== id))
  }

  function handleRefreshed(updated: Station) {
    setStations(prev => prev.map(s => s.id === updated.id ? updated : s))
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white font-[family-name:var(--font-family-display)]">
            Stations
          </h1>
          <p className="text-sm text-slate-400 mt-0.5">
            Sonic similarity playlists seeded by tracks · refreshes at 6 AM
          </p>
          {stats && (
            <div className="mt-2 flex items-center gap-2">
              <div className="h-1.5 w-48 bg-[#1a1d27] rounded-full overflow-hidden">
                <div
                  className="h-full bg-[#d4a017]/70 rounded-full transition-all duration-700"
                  style={{ width: `${stats.coverage_pct}%` }}
                />
              </div>
              <span className="text-[11px] text-slate-500">
                {stats.analyzed_count.toLocaleString()} / {stats.total_tracks.toLocaleString()} analyzed
                {stats.queued_count > 0 && ` · ${stats.queued_count} queued`}
              </span>
            </div>
          )}
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="w-4 h-4 mr-1.5" />
          New Station
        </Button>
      </div>

      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {[1, 2, 3].map(i => (
            <div key={i} className="h-36 bg-[#1a1d27] rounded-xl animate-pulse" />
          ))}
        </div>
      ) : stations.length === 0 ? (
        <EmptyState
          icon={Radio}
          title="No stations yet"
          description="Seed a station with 3–5 tracks that define the vibe. Music Machine will find similar-sounding music in your library using acoustic fingerprinting and generate a fresh playlist every morning."
          action={
            <Button onClick={() => setCreateOpen(true)}>
              <Plus className="w-4 h-4 mr-1.5" />
              Create First Station
            </Button>
          }
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {stations.map((station, i) => (
            <StationCard
              key={station.id}
              station={station}
              index={i}
              onDelete={handleDelete}
              onRefreshed={handleRefreshed}
            />
          ))}
        </div>
      )}

      <CreateStationModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={handleCreated}
      />
    </div>
  )
}
