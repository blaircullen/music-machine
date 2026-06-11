import { useState, useEffect } from 'react'
import { ArrowLeft, RotateCcw } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { GlassCard } from '../components/ui/GlassCard'
import { Badge } from '../components/ui/Badge'
import { getFingerprintHistory, rollbackFingerprint, type FingerprintHistoryItem } from '../lib/api'

export default function FingerprintHistory() {
  const navigate = useNavigate()
  const [items, setItems] = useState<FingerprintHistoryItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)

  const load = async () => {
    try {
      const data = await getFingerprintHistory(200)
      setItems(data.items)
      setTotal(data.total)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const handleRollback = async (id: number) => {
    if (!confirm('Rollback this track to its original tags?')) return
    await rollbackFingerprint(id)
    load()
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
      <div className="flex items-center gap-3">
        <button onClick={() => navigate('/fingerprint')} className="text-slate-400 hover:text-white transition-colors">
          <ArrowLeft className="w-5 h-5" />
        </button>
        <h1 className="text-xl font-bold text-white font-[family-name:var(--font-family-display)]">
          Change History
        </h1>
        <Badge variant="blue">{total}</Badge>
      </div>

      {items.length === 0 ? (
        <GlassCard>
          <div className="text-center py-8">
            <p className="text-slate-400">No tag changes recorded yet</p>
          </div>
        </GlassCard>
      ) : (
        <div className="space-y-1">
          {items.map(item => (
            <GlassCard key={item.id} className="!p-3">
              <div className="flex items-center justify-between">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-slate-400">
                      {item.original_artist || '?'} — {item.original_title || '?'}
                    </span>
                    <span className="text-slate-600 text-xs">&rarr;</span>
                    <span className="text-sm text-amber-200">
                      {item.matched_artist || '?'} — {item.matched_title || '?'}
                    </span>
                  </div>
                  <div className="flex items-center gap-3 mt-1 text-xs text-slate-500">
                    <span>{item.original_album || '?'} &rarr; {item.matched_album || '?'}</span>
                    {item.matched_genre && <Badge variant="gray">{item.matched_genre}</Badge>}
                    <span className="capitalize">{item.match_source}</span>
                    <span>{Math.round(item.composite_confidence * 100)}%</span>
                    <span className="ml-auto">{new Date(item.snapshot_at).toLocaleString()}</span>
                  </div>
                  <div className="text-[10px] text-slate-600 truncate mt-0.5">{item.file_path}</div>
                </div>
                {item.fp_status !== 'rolled_back' && (
                  <button
                    onClick={() => handleRollback(item.id)}
                    className="ml-3 p-1.5 text-slate-400 hover:text-red-400 hover:bg-red-400/10 rounded transition-colors"
                    title="Rollback"
                  >
                    <RotateCcw className="w-4 h-4" />
                  </button>
                )}
                {item.fp_status === 'rolled_back' && (
                  <Badge variant="red">Rolled Back</Badge>
                )}
              </div>
            </GlassCard>
          ))}
        </div>
      )}
    </div>
  )
}
