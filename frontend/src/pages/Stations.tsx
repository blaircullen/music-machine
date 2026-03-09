import { useState, useEffect, useCallback } from 'react'
import { Radio, Plus, RefreshCw, Trash2, Check, X } from 'lucide-react'
import { GlassCard } from '../components/ui/GlassCard'
import { Button } from '../components/ui/Button'
import { Badge } from '../components/ui/Badge'
import { Modal } from '../components/ui/Modal'
import { EmptyState } from '../components/ui/EmptyState'
import {
  getStations, createStation, deleteStation,
  refreshStation, getStationRefreshStatus,
  type Station, type StationCreate,
} from '../lib/api'
import toast from 'react-hot-toast'

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
  const [artistInput, setArtistInput] = useState('')
  const [seedArtists, setSeedArtists] = useState<string[]>([])
  const [bpmEnabled, setBpmEnabled] = useState(false)
  const [bpmMin, setBpmMin] = useState(120)
  const [bpmMax, setBpmMax] = useState(180)
  const [decadeEnabled, setDecadeEnabled] = useState(false)
  const [decade, setDecade] = useState('90s')
  const [saving, setSaving] = useState(false)

  const DECADE_RANGES: Record<string, [number, number]> = {
    '70s': [1970, 1979], '80s': [1980, 1989], '90s': [1990, 1999],
    '00s': [2000, 2009], '10s': [2010, 2019], '20s': [2020, 2029],
  }

  function reset() {
    setName(''); setArtistInput(''); setSeedArtists([])
    setBpmEnabled(false); setBpmMin(120); setBpmMax(180)
    setDecadeEnabled(false); setDecade('90s'); setSaving(false)
  }

  function handleClose() { reset(); onClose() }

  function addArtist() {
    const trimmed = artistInput.trim()
    if (trimmed && !seedArtists.includes(trimmed)) {
      setSeedArtists(prev => [...prev, trimmed])
    }
    setArtistInput('')
  }

  function removeArtist(artist: string) {
    setSeedArtists(prev => prev.filter(a => a !== artist))
  }

  async function handleSave() {
    if (!name.trim()) { toast.error('Station name required'); return }
    if (seedArtists.length === 0) { toast.error('Add at least one seed artist'); return }

    const data: StationCreate = {
      name: name.trim(),
      seed_artists: seedArtists,
      bpm_min: bpmEnabled ? bpmMin : null,
      bpm_max: bpmEnabled ? bpmMax : null,
    }
    if (decadeEnabled) {
      const [dMin, dMax] = DECADE_RANGES[decade]
      data.decade_min = dMin
      data.decade_max = dMax
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

  if (!open) return null

  return (
    <Modal title="New Station" onClose={handleClose}>
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

        {/* Seed Artists */}
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Seed Artists</label>
          <div className="flex gap-2">
            <input
              type="text"
              value={artistInput}
              onChange={e => setArtistInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addArtist() } }}
              placeholder="Artist name — press Enter to add"
              className="flex-1 bg-[#1a1d27] border border-[#2a2d3a] rounded-lg px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-[#d4a017]/50"
            />
            <Button variant="secondary" onClick={addArtist}>Add</Button>
          </div>
          {seedArtists.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-2">
              {seedArtists.map(a => (
                <span key={a} className="flex items-center gap-1 bg-[#d4a017]/15 text-[#f0c95c] text-xs px-2 py-1 rounded-full">
                  {a}
                  <button onClick={() => removeArtist(a)} className="hover:text-white transition-colors">
                    <X className="w-3 h-3" />
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>

        {/* BPM Range */}
        <div>
          <label className="flex items-center gap-2 text-xs text-slate-400 mb-2 cursor-pointer">
            <input
              type="checkbox"
              checked={bpmEnabled}
              onChange={e => setBpmEnabled(e.target.checked)}
              className="accent-[#d4a017]"
            />
            BPM Range (workout energy control)
          </label>
          {bpmEnabled && (
            <div className="flex items-center gap-3 pl-5">
              <div>
                <label className="text-[10px] text-slate-500 block mb-1">Min BPM</label>
                <input
                  type="number" min={60} max={220} value={bpmMin}
                  onChange={e => setBpmMin(Number(e.target.value))}
                  className="w-20 bg-[#1a1d27] border border-[#2a2d3a] rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-[#d4a017]/50"
                />
              </div>
              <span className="text-slate-500 mt-4">–</span>
              <div>
                <label className="text-[10px] text-slate-500 block mb-1">Max BPM</label>
                <input
                  type="number" min={60} max={220} value={bpmMax}
                  onChange={e => setBpmMax(Number(e.target.value))}
                  className="w-20 bg-[#1a1d27] border border-[#2a2d3a] rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-[#d4a017]/50"
                />
              </div>
            </div>
          )}
        </div>

        {/* Decade Filter */}
        <div>
          <label className="flex items-center gap-2 text-xs text-slate-400 mb-2 cursor-pointer">
            <input
              type="checkbox"
              checked={decadeEnabled}
              onChange={e => setDecadeEnabled(e.target.checked)}
              className="accent-[#d4a017]"
            />
            Decade Filter
          </label>
          {decadeEnabled && (
            <select
              value={decade}
              onChange={e => setDecade(e.target.value)}
              className="ml-5 bg-[#1a1d27] border border-[#2a2d3a] rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-[#d4a017]/50"
            >
              {Object.keys(DECADE_RANGES).map(d => (
                <option key={d} value={d}>{d}</option>
              ))}
            </select>
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
  onDelete: (id: number) => void
  onRefreshed: (station: Station) => void
}

function StationCard({ station, onDelete, onRefreshed }: StationCardProps) {
  const [refreshing, setRefreshing] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

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
    <GlassCard className="p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 mb-2">
            <Radio className="w-4 h-4 text-[#d4a017] shrink-0" />
            <h3 className="text-white font-semibold text-sm truncate font-[family-name:var(--font-family-display)]">
              {station.name}
            </h3>
          </div>

          <div className="flex flex-wrap gap-1 mb-3">
            {station.seed_artists.map(a => (
              <span key={a} className="text-[10px] bg-[#d4a017]/10 text-[#f0c95c] px-2 py-0.5 rounded-full">
                {a}
              </span>
            ))}
          </div>

          <div className="flex flex-wrap gap-1.5 mb-3">
            {station.bpm_min != null && station.bpm_max != null && (
              <Badge variant="info">{station.bpm_min}–{station.bpm_max} BPM</Badge>
            )}
            {station.decade_min != null && (
              <Badge variant="info">{String(station.decade_min).slice(2)}s</Badge>
            )}
          </div>

          <div className="text-[10px] text-slate-500 space-y-0.5">
            <div>
              <span className="text-slate-400">{station.track_count}</span> tracks
              {' · '}
              <span className="text-slate-400">{station.plex_playlist_name}</span>
            </div>
            <div>{lastRefreshed}</div>
          </div>
        </div>

        <div className="flex flex-col gap-1.5 shrink-0">
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
  )
}

// ------------------------------------------------------------------
// Stations page
// ------------------------------------------------------------------

export default function Stations() {
  const [stations, setStations] = useState<Station[]>([])
  const [loading, setLoading] = useState(true)
  const [createOpen, setCreateOpen] = useState(false)

  const load = useCallback(async () => {
    try {
      setStations(await getStations())
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
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white font-[family-name:var(--font-family-display)]">
            Stations
          </h1>
          <p className="text-sm text-slate-400 mt-0.5">
            Pandora-style daily playlists from your library · refreshes at 6 AM
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="w-4 h-4 mr-1.5" />
          New Station
        </Button>
      </div>

      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {[1, 2, 3].map(i => (
            <div key={i} className="h-40 bg-[#1a1d27] rounded-xl animate-pulse" />
          ))}
        </div>
      ) : stations.length === 0 ? (
        <EmptyState
          icon={Radio}
          title="No stations yet"
          description="Create a station by seeding it with artists you love. It will generate a fresh playlist daily."
          action={
            <Button onClick={() => setCreateOpen(true)}>
              <Plus className="w-4 h-4 mr-1.5" />
              Create First Station
            </Button>
          }
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {stations.map(station => (
            <StationCard
              key={station.id}
              station={station}
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
