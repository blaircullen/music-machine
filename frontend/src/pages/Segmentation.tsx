import { useEffect, useRef, useState, useCallback } from 'react'
import { Check, X, Play, RefreshCw, Loader2, ShieldAlert, ShieldCheck, Split } from 'lucide-react'
import { GlassCard, Button, Badge, EmptyState, toast } from '../components/ui'

// Live & Holiday segmentation review (docs/live-holiday-split-spec.md §6).
// Pending candidates with per-row approve/reject + apply-approved. Polls status via a
// useRef<setInterval> (repo leak-prevention pattern — App re-runs must never leak timers).

interface Candidate {
  id: number
  track_id: number
  source_path: string | null
  dest_path: string | null
  target_library: 'live' | 'holiday' | null
  matched_field: string | null
  matched_pattern: string | null
  confidence_tier: 'auto' | 'review' | null
  confidence_reason: string | null
  status: string
  artist: string | null
  album: string | null
  title: string | null
}

interface Status {
  running: boolean
  phase: string
  pending: number
  approved: number
  moved: number
  move_enabled: boolean
  sweep_enabled: boolean
  last_error: string | null
}

function targetBadge(t: string | null) {
  if (t === 'live') return <Badge variant="blue">Live</Badge>
  if (t === 'holiday') return <Badge variant="purple">Holiday</Badge>
  return <Badge variant="gray">—</Badge>
}

function tierBadge(tier: string | null) {
  return tier === 'auto'
    ? <Badge variant="green">auto</Badge>
    : <Badge variant="amber">review</Badge>
}

export default function Segmentation() {
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [status, setStatus] = useState<Status | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<Set<number>>(new Set())
  const [acting, setActing] = useState(false)
  // Repo leak-prevention pattern: hold the interval in a ref, clear it on unmount.
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadCandidates = useCallback(async () => {
    try {
      const res = await fetch('/api/segmentation/candidates?status=proposed&limit=500')
      if (!res.ok) throw new Error(await res.text())
      const data = await res.json()
      setCandidates(data.candidates ?? [])
    } catch {
      toast.error('Failed to load segmentation candidates')
    } finally {
      setLoading(false)
    }
  }, [])

  const loadStatus = useCallback(async () => {
    try {
      const res = await fetch('/api/segmentation/status')
      if (!res.ok) return
      setStatus(await res.json())
    } catch {
      // ignore transient poll errors
    }
  }, [])

  useEffect(() => {
    loadCandidates()
    loadStatus()
    pollRef.current = setInterval(loadStatus, 2000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [loadCandidates, loadStatus])

  const decide = async (id: number, action: 'approve' | 'reject') => {
    setBusy(prev => new Set(prev).add(id))
    try {
      const res = await fetch(`/api/segmentation/candidates/${id}/${action}`, { method: 'POST' })
      if (!res.ok) {
        const detail = await res.json().then(d => d?.detail).catch(() => null)
        throw new Error(detail || `${action} failed (${res.status})`)
      }
      setCandidates(prev => prev.filter(c => c.id !== id))
      loadStatus()
    } catch (e) {
      toast.error(`${action} failed: ${e instanceof Error ? e.message : 'error'}`)
    } finally {
      setBusy(prev => { const n = new Set(prev); n.delete(id); return n })
    }
  }

  const runDryRun = async () => {
    setActing(true)
    try {
      const res = await fetch('/api/segmentation/dry-run', { method: 'POST' })
      if (!res.ok) throw new Error(await res.text())
      toast.success('Dry-run started — candidates will populate as it classifies')
      setTimeout(loadCandidates, 1500)
    } catch (e) {
      toast.error(`Dry-run failed: ${e instanceof Error ? e.message : 'error'}`)
    } finally {
      setActing(false)
    }
  }

  const applyApproved = async () => {
    setActing(true)
    try {
      const res = await fetch('/api/segmentation/apply-approved', { method: 'POST' })
      if (!res.ok) {
        if (res.status === 403) {
          toast.error('Apply blocked — enable segmentation_move_enabled in Settings')
        } else {
          throw new Error(await res.text())
        }
        return
      }
      toast.success('Applying approved moves — watch status')
      loadStatus()
    } catch (e) {
      toast.error(`Apply failed: ${e instanceof Error ? e.message : 'error'}`)
    } finally {
      setActing(false)
    }
  }

  const moveArmed = status?.move_enabled ?? false

  return (
    <div className="space-y-6 max-w-6xl">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Split className="w-6 h-6 text-[#d4a017]" />
          <h2 className="text-2xl font-bold font-[family-name:var(--font-family-display)]">
            Live &amp; Holiday Split
          </h2>
          {status && <Badge variant="default">{status.pending} pending</Badge>}
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={runDryRun} disabled={acting || status?.running}>
            {status?.running ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            Dry-run
          </Button>
          <span title={moveArmed ? 'Move approved candidates' : 'Enable segmentation_move_enabled in Settings'}>
            <Button variant="primary" onClick={applyApproved} disabled={acting || !moveArmed || (status?.approved ?? 0) === 0}>
              <Play className="w-4 h-4" /> Apply Approved ({status?.approved ?? 0})
            </Button>
          </span>
        </div>
      </div>

      {/* Kill-switch banner */}
      <GlassCard className={`p-4 border ${moveArmed ? 'border-[#22c55e]/30 bg-[#22c55e]/5' : 'border-[#f59e0b]/30 bg-[#f59e0b]/5'}`}>
        <div className="flex items-start gap-3">
          {moveArmed
            ? <ShieldCheck className="w-5 h-5 text-[#4ade80] shrink-0 mt-0.5" />
            : <ShieldAlert className="w-5 h-5 text-[#fbbf24] shrink-0 mt-0.5" />}
          <div className="space-y-1">
            <p className="text-sm font-medium">
              {moveArmed
                ? 'Moves ARMED — approved & auto-tier candidates can be physically relocated.'
                : 'Review-only — physical moves are disabled. Dry-run and approve/reject are always safe.'}
            </p>
            <p className="text-xs text-slate-400">
              Physical moves require{' '}
              <span className={moveArmed ? 'text-[#4ade80]' : 'text-[#f87171]'}>
                segmentation_move_enabled={String(moveArmed)}
              </span>
              {' · '}sweep: {String(status?.sweep_enabled ?? false)}
              {' · '}moved so far: {status?.moved ?? 0}
            </p>
            {status?.last_error && (
              <p className="text-xs text-[#f87171]">Last error: {status.last_error}</p>
            )}
          </div>
        </div>
      </GlassCard>

      {loading ? (
        <div className="flex items-center justify-center h-64">
          <div className="w-5 h-5 border-2 border-[#d4a017] border-t-transparent rounded-full animate-spin" />
        </div>
      ) : candidates.length === 0 ? (
        <EmptyState
          icon={Split}
          title="No pending candidates"
          description="Run a dry-run to classify live-performance and holiday tracks into the review queue."
        />
      ) : (
        <GlassCard className="overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-[#2a2d3a] text-slate-400 text-left bg-[#13151f]/40">
                <th scope="col" className="px-4 py-3 font-medium">Track</th>
                <th scope="col" className="px-4 py-3 font-medium">Library</th>
                <th scope="col" className="px-4 py-3 font-medium">Tier</th>
                <th scope="col" className="px-4 py-3 font-medium">Match</th>
                <th scope="col" className="px-4 py-3 font-medium">Source → Dest</th>
                <th scope="col" className="px-4 py-3 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map(c => (
                <tr key={c.id} className="border-b border-[#2a2d3a]/50 hover:bg-white/[0.02] transition-colors align-top">
                  <td className="px-4 py-3">
                    <div className="text-white truncate max-w-[16rem]">{c.artist ?? '—'} — {c.title ?? '—'}</div>
                    <div className="text-xs text-slate-500 truncate max-w-[16rem]">{c.album ?? ''}</div>
                  </td>
                  <td className="px-4 py-3">{targetBadge(c.target_library)}</td>
                  <td className="px-4 py-3">{tierBadge(c.confidence_tier)}</td>
                  <td className="px-4 py-3">
                    <div className="text-xs text-slate-300">{c.matched_field}: <span className="font-mono">{c.matched_pattern}</span></div>
                    <div className="text-[11px] text-slate-500 max-w-[18rem]">{c.confidence_reason}</div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="text-[11px] text-slate-500 font-mono truncate max-w-[20rem]" title={c.source_path ?? ''}>{c.source_path}</div>
                    <div className="text-[11px] text-amber-300/80 font-mono truncate max-w-[20rem]" title={c.dest_path ?? ''}>→ {c.dest_path ?? '(no dest)'}</div>
                  </td>
                  <td className="px-4 py-3 text-right whitespace-nowrap">
                    <div className="inline-flex gap-1">
                      <button
                        onClick={() => decide(c.id, 'approve')}
                        disabled={busy.has(c.id)}
                        className="p-1.5 rounded text-emerald-400 hover:bg-emerald-400/10 transition-colors disabled:opacity-40"
                        title="Approve"
                      >
                        {busy.has(c.id) ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                      </button>
                      <button
                        onClick={() => decide(c.id, 'reject')}
                        disabled={busy.has(c.id)}
                        className="p-1.5 rounded text-slate-400 hover:bg-slate-400/10 transition-colors disabled:opacity-40"
                        title="Reject"
                      >
                        <X className="w-4 h-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </GlassCard>
      )}
    </div>
  )
}
