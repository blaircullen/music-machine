import { useState, useEffect } from 'react'
import { ArrowLeft, Check, SkipForward, ChevronDown, ChevronUp } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { GlassCard } from '../components/ui/GlassCard'
import { Button } from '../components/ui/Button'
import { Badge } from '../components/ui/Badge'
import {
  getFingerprintReview, approveFingerprintResult, skipFingerprintResult,
  batchApproveFingerprintResults,
  type FingerprintReviewItem,
} from '../lib/api'

function ConfidenceBadge({ value }: { value: number }) {
  const pct = Math.round(value * 100)
  const variant = pct >= 90 ? 'green' as const : pct >= 70 ? 'amber' as const : 'orange' as const
  return <Badge variant={variant}>{pct}%</Badge>
}

function DiffField({ label, current, matched }: { label: string; current: string | null; matched: string | null }) {
  const changed = current !== matched && matched
  return (
    <div className="grid grid-cols-[100px_1fr_1fr] gap-2 text-xs py-1 border-b border-slate-800 last:border-0">
      <span className="text-slate-500">{label}</span>
      <span className="text-slate-400">{current || '—'}</span>
      <span className={changed ? 'text-amber-300 font-medium' : 'text-slate-400'}>
        {matched || '—'}
      </span>
    </div>
  )
}

export default function FingerprintReview() {
  const navigate = useNavigate()
  const [items, setItems] = useState<FingerprintReviewItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())

  const load = async () => {
    try {
      const data = await getFingerprintReview({ status: 'flagged', limit: 100 })
      setItems(data.items)
      setTotal(data.total)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const handleApprove = async (id: number) => {
    await approveFingerprintResult(id)
    setItems(prev => prev.filter(i => i.id !== id))
    setTotal(prev => prev - 1)
  }

  const handleSkip = async (id: number) => {
    await skipFingerprintResult(id)
    setItems(prev => prev.filter(i => i.id !== id))
    setTotal(prev => prev - 1)
  }

  const handleBatchApprove = async () => {
    if (selected.size > 0) {
      await batchApproveFingerprintResults({ ids: Array.from(selected) })
    } else {
      await batchApproveFingerprintResults({ min_confidence: 0.90 })
    }
    load()
    setSelected(new Set())
  }

  const toggleSelect = (id: number) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="w-5 h-5 border-2 border-[#d4a017] border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  return (
    <div className="space-y-4 max-w-6xl">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button onClick={() => navigate('/fingerprint')} className="text-slate-400 hover:text-white transition-colors">
            <ArrowLeft className="w-5 h-5" />
          </button>
          <h1 className="text-xl font-bold text-white font-[family-name:var(--font-family-display)]">
            Review Queue
          </h1>
          <Badge variant="default">{total}</Badge>
        </div>
        <div className="flex gap-2">
          <Button onClick={handleBatchApprove} size="sm">
            <Check className="w-3.5 h-3.5 mr-1.5" />
            {selected.size > 0 ? `Approve ${selected.size} Selected` : 'Approve All >= 90%'}
          </Button>
        </div>
      </div>

      {items.length === 0 ? (
        <GlassCard>
          <div className="text-center py-8">
            <p className="text-slate-400">No tracks pending review</p>
          </div>
        </GlassCard>
      ) : (
        <div className="space-y-2">
          {/* Header */}
          <div className="grid grid-cols-[32px_1fr_1fr_80px_70px_90px] gap-2 px-3 text-xs text-slate-500 font-medium">
            <span />
            <span>Current Tags</span>
            <span>Fingerprint Match</span>
            <span className="text-right">Confidence</span>
            <span className="text-right">Source</span>
            <span />
          </div>

          {items.map((item) => (
            <div key={item.id}>
              <GlassCard className="!p-0">
                <div
                  className="grid grid-cols-[32px_1fr_1fr_80px_70px_90px] gap-2 items-center px-3 py-2.5 cursor-pointer hover:bg-white/[0.02] transition-colors"
                  onClick={() => setExpandedId(expandedId === item.id ? null : item.id)}
                >
                  <input
                    type="checkbox"
                    checked={selected.has(item.id)}
                    onChange={(e) => { e.stopPropagation(); toggleSelect(item.id) }}
                    className="w-4 h-4 rounded border-slate-600 bg-slate-800"
                  />
                  <div className="min-w-0">
                    <div className="text-sm text-white truncate">{item.current_artist} — {item.current_title}</div>
                    <div className="text-xs text-slate-500 truncate">{item.current_album}</div>
                  </div>
                  <div className="min-w-0">
                    <div className="text-sm text-amber-200 truncate">{item.matched_artist} — {item.matched_title}</div>
                    <div className="text-xs text-slate-500 truncate">{item.matched_album}</div>
                  </div>
                  <div className="text-right">
                    <ConfidenceBadge value={item.composite_confidence} />
                  </div>
                  <div className="text-right text-xs text-slate-400 capitalize">
                    {item.match_source}
                  </div>
                  <div className="flex items-center gap-1 justify-end">
                    <button
                      onClick={(e) => { e.stopPropagation(); handleApprove(item.id) }}
                      className="p-1 rounded text-emerald-400 hover:bg-emerald-400/10 transition-colors"
                      title="Approve"
                    >
                      <Check className="w-4 h-4" />
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); handleSkip(item.id) }}
                      className="p-1 rounded text-slate-400 hover:bg-slate-400/10 transition-colors"
                      title="Skip"
                    >
                      <SkipForward className="w-4 h-4" />
                    </button>
                    {expandedId === item.id ? (
                      <ChevronUp className="w-4 h-4 text-slate-500" />
                    ) : (
                      <ChevronDown className="w-4 h-4 text-slate-500" />
                    )}
                  </div>
                </div>

                {/* Expanded Detail */}
                {expandedId === item.id && (
                  <div className="border-t border-slate-800 px-4 py-3 bg-slate-900/50">
                    <div className="grid grid-cols-[100px_1fr_1fr] gap-2 text-xs mb-2">
                      <span className="text-slate-600 font-medium">Field</span>
                      <span className="text-slate-600 font-medium">Current</span>
                      <span className="text-slate-600 font-medium">Match</span>
                    </div>
                    <DiffField label="Artist" current={item.current_artist} matched={item.matched_artist} />
                    <DiffField label="Title" current={item.current_title} matched={item.matched_title} />
                    <DiffField label="Album" current={item.current_album} matched={item.matched_album} />
                    <DiffField label="Album Artist" current={item.current_album_artist} matched={item.matched_album_artist} />
                    <DiffField label="Year" current={null} matched={item.matched_year?.toString() ?? null} />
                    <DiffField label="Track #" current={item.current_track_number?.toString() ?? null} matched={item.matched_track_number?.toString() ?? null} />
                    <DiffField label="Genre" current={null} matched={item.matched_genre} />
                    <DiffField label="ISRC" current={null} matched={item.matched_isrc} />
                    <DiffField label="Label" current={null} matched={item.matched_label} />
                    <DiffField label="Composer" current={null} matched={item.matched_composer} />
                    <div className="mt-3 text-xs text-slate-600 truncate">
                      {item.file_path}
                    </div>
                    {item.matched_cover_art_url && (
                      <div className="mt-2">
                        <img src={item.matched_cover_art_url} alt="Cover" className="w-24 h-24 rounded object-cover" />
                      </div>
                    )}
                  </div>
                )}
              </GlassCard>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
