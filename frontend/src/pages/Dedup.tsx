import { useEffect, useState, useCallback } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { Layers, RefreshCw, Trash2, ChevronRight, Loader2, ShieldAlert, ShieldCheck } from 'lucide-react'
import { GlassCard, Button, Badge, EmptyState, SkeletonTable, Modal, toast } from '../components/ui'

// Shape of each track summary returned by dedup_pass._track_summary (backend/dedup_pass.py).
interface DedupTrack {
  id: number
  file_path: string | null
  format: string
  quality_score: number
  artist: string | null
  title: string | null
  album: string | null
  duration: number | null
  identity_state: string | null
  mb_recording_id: string | null
}

// Shape of each candidate group returned by dedup_pass._classify_group.
interface DedupCandidate {
  match_type: string
  confidence: number
  skipped: boolean
  skip_reason: string | null
  auto_eligible: boolean
  keep_id: number
  keep: DedupTrack
  trash_ids: number[]
  trash: DedupTrack[]
}

interface CandidatesResponse {
  candidates: DedupCandidate[]
  count: number
  auto_eligible: number
  identity_act_enabled: boolean
  dedup_act_enabled: boolean
}

interface ApplyResult {
  dry_run: boolean
  keep_id: number
  applied: { trashed_id: number; path?: string }[]
  skipped: { trashed_id: number; reason: string }[]
  errors: { trashed_id: number; error: string }[]
}

type FilterTab = 'actionable' | 'auto' | 'skipped'

// Stable React key for a candidate (the backend returns no group id).
function groupKey(c: DedupCandidate): string {
  return `${c.keep_id}-${[...c.trash_ids].sort((a, b) => a - b).join(',')}`
}

function formatDuration(sec: number | null): string {
  if (!sec || sec <= 0) return '-'
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

function identityBadge(state: string | null) {
  switch (state) {
    case 'confirmed': return <Badge variant="green">confirmed</Badge>
    case 'review': return <Badge variant="amber">review</Badge>
    case 'conflict': return <Badge variant="red">conflict</Badge>
    case 'resolved': return <Badge variant="blue">resolved</Badge>
    default: return <Badge variant="gray">unresolved</Badge>
  }
}

const LOSSLESS = new Set(['flac', 'alac', 'wav'])

export default function Dedup() {
  const [candidates, setCandidates] = useState<DedupCandidate[]>([])
  const [loading, setLoading] = useState(true)
  const [filterTab, setFilterTab] = useState<FilterTab>('actionable')
  const [expandedKey, setExpandedKey] = useState<string | null>(null)
  const [applying, setApplying] = useState<Set<string>>(new Set())
  const [identityEnabled, setIdentityEnabled] = useState(false)
  const [dedupEnabled, setDedupEnabled] = useState(false)
  const [confirmGroup, setConfirmGroup] = useState<DedupCandidate | null>(null)

  // include_skipped=true loads everything in one call; tabs filter client-side so switching
  // tabs never refetches (the scan over the whole active library is the expensive part).
  const fetchCandidates = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetch('/api/dedup/candidates?include_skipped=true')
      if (!res.ok) throw new Error(await res.text())
      const data: CandidatesResponse = await res.json()
      setCandidates(data.candidates ?? [])
      setIdentityEnabled(data.identity_act_enabled)
      setDedupEnabled(data.dedup_act_enabled)
    } catch {
      toast.error('Failed to load dedup candidates')
      setCandidates([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchCandidates() }, [fetchCandidates])

  const armed = identityEnabled && dedupEnabled

  // dry_run verify: server re-derives the group + gate from the ids; surfaces a 400 if the
  // displayed keep/trash no longer forms a single valid group. Never touches files.
  const handleVerify = async (c: DedupCandidate) => {
    const key = groupKey(c)
    setApplying(prev => new Set(prev).add(key))
    try {
      const res = await fetch('/api/dedup/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ keep_id: c.keep_id, trash_ids: c.trash_ids, dry_run: true }),
      })
      // fetch() resolves on 4xx/5xx too — check res.ok before using the body.
      if (!res.ok) {
        const detail = await res.json().then(d => d?.detail).catch(() => null)
        throw new Error(detail || `verify failed (${res.status})`)
      }
      const data: ApplyResult = await res.json()
      toast.success(`Verified — would trash ${data.applied.length} file(s), keep #${data.keep_id}`)
    } catch (e) {
      toast.error(`Verify failed: ${e instanceof Error ? e.message : 'error'}`)
    } finally {
      setApplying(prev => { const n = new Set(prev); n.delete(key); return n })
    }
  }

  const handleApply = async (c: DedupCandidate) => {
    setConfirmGroup(null)
    const key = groupKey(c)
    setApplying(prev => new Set(prev).add(key))
    try {
      const res = await fetch('/api/dedup/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ keep_id: c.keep_id, trash_ids: c.trash_ids, dry_run: false }),
      })
      // fetch() resolves on 4xx/5xx too — check res.ok before using the body.
      if (!res.ok) {
        if (res.status === 403) {
          toast.error('Apply blocked — enable identity_act_enabled AND dedup_act_enabled in Settings')
        } else {
          const detail = await res.json().then(d => d?.detail).catch(() => null)
          throw new Error(detail || `apply failed (${res.status})`)
        }
        return
      }
      const data: ApplyResult = await res.json()
      if (data.errors.length > 0) {
        toast.error(`Trashed ${data.applied.length}, ${data.errors.length} error(s) — check Job Log`)
      } else {
        toast.success(`Trashed ${data.applied.length} inferior copy(ies) — kept #${data.keep_id}. Refresh Plex section 5.`)
      }
      // Drop the group from view; trashed members are no longer active.
      setCandidates(prev => prev.filter(x => groupKey(x) !== key))
      if (expandedKey === key) setExpandedKey(null)
    } catch (e) {
      toast.error(`Apply failed: ${e instanceof Error ? e.message : 'error'}`)
    } finally {
      setApplying(prev => { const n = new Set(prev); n.delete(key); return n })
    }
  }

  const actionable = candidates.filter(c => !c.skipped)
  const autoEligible = actionable.filter(c => c.auto_eligible)
  const skipped = candidates.filter(c => c.skipped)

  const visible =
    filterTab === 'auto' ? autoEligible :
    filterTab === 'skipped' ? skipped :
    actionable

  const tabs: { key: FilterTab; label: string; count: number }[] = [
    { key: 'actionable', label: 'Actionable', count: actionable.length },
    { key: 'auto', label: 'Auto-eligible', count: autoEligible.length },
    { key: 'skipped', label: 'Gated out', count: skipped.length },
  ]

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold font-[family-name:var(--font-family-display)]">Dedup Review</h2>
        <Button variant="secondary" onClick={fetchCandidates} disabled={loading}>
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          {loading ? 'Scanning…' : 'Rescan'}
        </Button>
      </div>

      {/* Gate banner — apply is fail-closed unless BOTH flags are on (review-only by default). */}
      <GlassCard className={`p-4 border ${armed ? 'border-[#22c55e]/30 bg-[#22c55e]/5' : 'border-[#f59e0b]/30 bg-[#f59e0b]/5'}`}>
        <div className="flex items-start gap-3">
          {armed
            ? <ShieldCheck className="w-5 h-5 text-[#4ade80] shrink-0 mt-0.5" />
            : <ShieldAlert className="w-5 h-5 text-[#fbbf24] shrink-0 mt-0.5" />}
          <div className="space-y-1">
            <p className="text-sm font-medium">
              {armed
                ? 'Apply is ARMED — trashing inferior copies is enabled.'
                : 'Review-only — Apply is disabled. Verify is always safe.'}
            </p>
            <p className="text-xs text-base-400">
              Trashing requires BOTH gates ON (flip them deliberately in Settings):{' '}
              <span className={identityEnabled ? 'text-[#4ade80]' : 'text-[#f87171]'}>
                identity_act_enabled={String(identityEnabled)}
              </span>{' · '}
              <span className={dedupEnabled ? 'text-[#4ade80]' : 'text-[#f87171]'}>
                dedup_act_enabled={String(dedupEnabled)}
              </span>
            </p>
          </div>
        </div>
      </GlassCard>

      {confirmGroup && (
        <Modal
          open={!!confirmGroup}
          onClose={() => setConfirmGroup(null)}
          title="Trash inferior copies"
          message={`Reversibly trash ${confirmGroup.trash_ids.length} file(s) and keep the ${confirmGroup.keep.format?.toUpperCase()} copy of "${confirmGroup.keep.artist} — ${confirmGroup.keep.title}". Trash is journaled and restorable.`}
          confirmLabel="Trash"
          confirmVariant="danger"
          onConfirm={() => handleApply(confirmGroup)}
        />
      )}

      <div className="flex gap-2 mb-4">
        {tabs.map(tab => (
          <button
            key={tab.key}
            onClick={() => setFilterTab(tab.key)}
            className={`px-3 py-1.5 rounded-xl text-sm font-medium transition-all duration-300 relative ${filterTab === tab.key
                ? 'text-accent drop-shadow-[0_0_8px_rgba(212,160,23,0.5)]'
                : 'text-base-500 hover:text-base-300 hover:bg-base-700/50'
              }`}
          >
            {tab.label} ({tab.count})
            {filterTab === tab.key && (
              <motion.div layoutId="dedupTabIndicator" className="absolute -bottom-1 left-3 right-3 h-0.5 bg-accent rounded-full shadow-[0_0_8px_rgba(212,160,23,0.8)]" />
            )}
          </button>
        ))}
      </div>

      {loading ? (
        <SkeletonTable rows={6} cols={7} />
      ) : visible.length === 0 ? (
        <EmptyState
          icon={Layers}
          title={filterTab === 'skipped' ? 'No gated-out groups' : 'No duplicate groups'}
          description={
            filterTab === 'skipped'
              ? 'Groups with an identity conflict or divergent recordings would appear here.'
              : 'Rescan the active library to detect lossy/lossless duplicate groups.'
          }
        />
      ) : (
        <GlassCard className="overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-glass-border/40 text-base-400 text-left bg-base-800/20 backdrop-blur-sm">
                <th scope="col" className="px-4 py-4 font-medium w-8"></th>
                <th scope="col" className="px-4 py-4 font-medium">Artist</th>
                <th scope="col" className="px-4 py-4 font-medium">Title</th>
                <th scope="col" className="px-4 py-4 font-medium text-center">Members</th>
                <th scope="col" className="px-4 py-4 font-medium text-center">Match</th>
                <th scope="col" className="px-4 py-4 font-medium text-center">Confidence</th>
                <th scope="col" className="px-4 py-4 font-medium text-center">
                  {filterTab === 'skipped' ? 'Reason' : 'Bucket'}
                </th>
                {filterTab !== 'skipped' && <th scope="col" className="px-4 py-4 font-medium text-right">Actions</th>}
              </tr>
            </thead>
            <tbody>
              <AnimatePresence>
                {visible.map((c) => {
                  const key = groupKey(c)
                  return (
                    <GroupRows
                      key={key}
                      cand={c}
                      isExpanded={expandedKey === key}
                      isApplying={applying.has(key)}
                      armed={armed}
                      onToggle={() => setExpandedKey(expandedKey === key ? null : key)}
                      onVerify={() => handleVerify(c)}
                      onApply={() => setConfirmGroup(c)}
                    />
                  )
                })}
              </AnimatePresence>
            </tbody>
          </table>
        </GlassCard>
      )}
    </div>
  )
}

function GroupRows({
  cand, isExpanded, isApplying, armed, onToggle, onVerify, onApply,
}: {
  cand: DedupCandidate
  isExpanded: boolean
  isApplying: boolean
  armed: boolean
  onToggle: () => void
  onVerify: () => void
  onApply: () => void
}) {
  const members = 1 + cand.trash.length
  const allTracks = [cand.keep, ...cand.trash]
  return (
    <>
      <motion.tr
        layout
        exit={{ opacity: 0, height: 0 }}
        className="border-b border-glass-border/30 hover:bg-white/[0.02] cursor-pointer transition-colors group"
        onClick={onToggle}
      >
        <td className="px-4 py-4 text-base-500 group-hover:text-accent transition-colors">
          <motion.span animate={{ rotate: isExpanded ? 90 : 0 }} className="inline-block">
            <ChevronRight className="w-5 h-5 drop-shadow-[0_0_8px_rgba(212,160,23,0.3)]" />
          </motion.span>
        </td>
        <td className="px-4 py-4 text-base-300 font-medium">{cand.keep.artist ?? '-'}</td>
        <td className="px-4 py-4 text-base-300">{cand.keep.title ?? '-'}</td>
        <td className="px-4 py-4 text-center">
          <span className="bg-base-700/60 px-2 py-1 rounded-md text-base-300">{members}</span>
        </td>
        <td className="px-4 py-4 text-center"><Badge>{cand.match_type}</Badge></td>
        <td className="px-4 py-4 text-center text-base-300">
          {cand.confidence > 0 ? `${(cand.confidence * 100).toFixed(0)}%` : '-'}
        </td>
        <td className="px-4 py-4 text-center">
          {cand.skipped
            ? <Badge variant="red">{cand.skip_reason}</Badge>
            : cand.auto_eligible
              ? <Badge variant="green">auto</Badge>
              : <Badge variant="gray">review</Badge>}
        </td>
        {!cand.skipped && (
          <td className="px-4 py-4 text-right whitespace-nowrap" onClick={e => e.stopPropagation()}>
            <div className="inline-flex gap-2">
              <Button size="sm" variant="ghost" onClick={onVerify} disabled={isApplying}>
                {isApplying ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Verify'}
              </Button>
              <span title={armed ? 'Trash inferior copies' : 'Enable both gates in Settings to apply'}>
                <Button size="sm" variant="danger" onClick={onApply} disabled={isApplying || !armed}>
                  <Trash2 className="w-4 h-4" /> Trash
                </Button>
              </span>
            </div>
          </td>
        )}
      </motion.tr>
      <AnimatePresence>
        {isExpanded && (
          <motion.tr
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="border-b border-glass-border/50"
          >
            <td colSpan={cand.skipped ? 7 : 8} className="p-0">
              <div className="bg-base-800/50 p-4 shadow-inner backdrop-blur-md">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-base-500 text-left border-b border-base-700/50">
                      <th scope="col" className="px-3 py-3 font-medium uppercase tracking-wider">Status</th>
                      <th scope="col" className="px-3 py-3 font-medium uppercase tracking-wider">Format</th>
                      <th scope="col" className="px-3 py-3 font-medium uppercase tracking-wider">Quality</th>
                      <th scope="col" className="px-3 py-3 font-medium uppercase tracking-wider">Duration</th>
                      <th scope="col" className="px-3 py-3 font-medium uppercase tracking-wider">Identity</th>
                      <th scope="col" className="px-3 py-3 font-medium uppercase tracking-wider">Recording</th>
                      <th scope="col" className="px-3 py-3 font-medium uppercase tracking-wider">File Path</th>
                    </tr>
                  </thead>
                  <tbody>
                    {allTracks.map(track => {
                      const isKeep = track.id === cand.keep_id
                      const lossless = LOSSLESS.has((track.format || '').toLowerCase())
                      return (
                        <tr
                          key={track.id}
                          className={`border-b border-base-700/30 transition-colors ${isKeep
                            ? 'bg-accent/5 border-l-2 border-l-lime hover:bg-accent/10'
                            : 'bg-transparent hover:bg-base-700/30 border-l-2 border-l-transparent'
                            }`}
                        >
                          <td className="px-3 py-2">
                            {isKeep ? <Badge variant="green">KEEP</Badge> : <Badge variant="red">TRASH</Badge>}
                          </td>
                          <td className="px-3 py-2 uppercase font-mono">
                            <span className={lossless ? 'text-[#4ade80]' : 'text-orange-400'}>{track.format || '-'}</span>
                          </td>
                          <td className="px-3 py-2 font-mono">{Math.round(track.quality_score)}</td>
                          <td className="px-3 py-2 font-mono">{formatDuration(track.duration)}</td>
                          <td className="px-3 py-2">{identityBadge(track.identity_state)}</td>
                          <td className="px-3 py-2 font-mono text-base-500 max-w-[10rem] truncate" title={track.mb_recording_id ?? ''}>
                            {track.mb_recording_id ?? '-'}
                          </td>
                          <td className="px-3 py-2 font-mono text-base-500 max-w-xs truncate" title={track.file_path ?? ''}>
                            {track.file_path ?? '-'}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
                {cand.skipped && (
                  <p className="mt-3 text-xs text-[#f87171]">
                    Gated out ({cand.skip_reason}) — this group will never be auto-trashed. Resolve the identity first.
                  </p>
                )}
              </div>
            </td>
          </motion.tr>
        )}
      </AnimatePresence>
    </>
  )
}
