import { useState, useEffect } from 'react'
import { ArrowLeft } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { GlassCard } from '../components/ui/GlassCard'
import { Badge } from '../components/ui/Badge'
import { getFingerprintUnmatched, skipFingerprintResult, type FingerprintReviewItem } from '../lib/api'

export default function FingerprintUnmatched() {
  const navigate = useNavigate()
  const [items, setItems] = useState<FingerprintReviewItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)

  const load = async () => {
    try {
      const data = await getFingerprintUnmatched(200)
      setItems(data.items)
      setTotal(data.total)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const handleSkip = async (id: number) => {
    await skipFingerprintResult(id)
    setItems(prev => prev.filter(i => i.id !== id))
    setTotal(prev => prev - 1)
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
          Unmatched Tracks
        </h1>
        <Badge variant="orange">{total}</Badge>
      </div>

      {items.length === 0 ? (
        <GlassCard>
          <div className="text-center py-8">
            <p className="text-slate-400">No unmatched tracks</p>
          </div>
        </GlassCard>
      ) : (
        <div className="space-y-1">
          {items.map(item => (
            <GlassCard key={item.id} className="!p-3">
              <div className="flex items-center justify-between">
                <div className="min-w-0 flex-1">
                  <div className="text-sm text-white truncate">
                    {item.current_artist || 'Unknown'} — {item.current_title || 'Unknown'}
                  </div>
                  <div className="text-xs text-slate-500 truncate mt-0.5">
                    {item.current_album || 'Unknown Album'} &middot; {item.format?.toUpperCase()} &middot;{' '}
                    {item.duration ? `${Math.floor(item.duration / 60)}:${String(Math.floor(item.duration % 60)).padStart(2, '0')}` : '—'}
                  </div>
                  <div className="text-[10px] text-slate-600 truncate mt-0.5">{item.file_path}</div>
                </div>
                <button
                  onClick={() => handleSkip(item.id)}
                  className="ml-3 px-3 py-1 text-xs text-slate-400 hover:text-white bg-slate-800 hover:bg-slate-700 rounded transition-colors"
                >
                  Mark Verified
                </button>
              </div>
            </GlassCard>
          ))}
        </div>
      )}
    </div>
  )
}
