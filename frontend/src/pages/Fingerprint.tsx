import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { Fingerprint, Play, Square, Eye, History, AlertCircle, CheckCircle, Database } from 'lucide-react'
import { GlassCard } from '../components/ui/GlassCard'
import { Button } from '../components/ui/Button'
import { Badge } from '../components/ui/Badge'
import { ProgressBar } from '../components/ui/ProgressBar'
import {
  getFingerprintStats, getFingerprintProgress, postFingerprintRun,
  stopFingerprintEngine, getMbStatus,
  type FingerprintStats, type FingerprintProgress,
} from '../lib/api'

function StatCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="text-center">
      <div className="text-2xl font-bold text-white font-[family-name:var(--font-family-display)]">{value}</div>
      <div className="text-xs text-slate-400 mt-1">{label}</div>
      {sub && <div className="text-[10px] text-slate-500 mt-0.5">{sub}</div>}
    </div>
  )
}

export default function FingerprintDashboard() {
  const navigate = useNavigate()
  const [stats, setStats] = useState<FingerprintStats | null>(null)
  const [progress, setProgress] = useState<FingerprintProgress | null>(null)
  const [mbAvailable, setMbAvailable] = useState<boolean | null>(null)
  const [loading, setLoading] = useState(true)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchAll = async () => {
    try {
      const [s, p, mb] = await Promise.all([
        getFingerprintStats(),
        getFingerprintProgress(),
        getMbStatus(),
      ])
      setStats(s)
      setProgress(p)
      setMbAvailable(mb.available)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchAll()
    pollRef.current = setInterval(fetchAll, 3000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  const handleRun = async (dryRun: boolean) => {
    await postFingerprintRun(dryRun)
    fetchAll()
  }

  const handleStop = async () => {
    await stopFingerprintEngine()
    fetchAll()
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="w-5 h-5 border-2 border-[#d4a017] border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  const isRunning = progress?.running ?? false
  const phaseName = progress?.phase ?? 'idle'

  return (
    <div className="space-y-6 max-w-6xl">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Fingerprint className="w-6 h-6 text-[#d4a017]" />
          <h1 className="text-xl font-bold text-white font-[family-name:var(--font-family-display)]">
            Fingerprint Verification
          </h1>
        </div>
        <div className="flex items-center gap-2">
          {isRunning ? (
            <Button onClick={handleStop} variant="danger" size="sm">
              <Square className="w-3.5 h-3.5 mr-1.5" /> Stop
            </Button>
          ) : (
            <>
              <Button onClick={() => handleRun(true)} variant="secondary" size="sm">
                Dry Run
              </Button>
              <Button onClick={() => handleRun(false)} size="sm">
                <Play className="w-3.5 h-3.5 mr-1.5" /> Run Audit
              </Button>
            </>
          )}
        </div>
      </div>

      {/* Progress bar when running */}
      {isRunning && progress && (
        <GlassCard>
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className="w-2 h-2 bg-emerald-400 rounded-full animate-pulse" />
                <span className="text-sm text-white font-medium capitalize">
                  {phaseName}{progress.dry_run ? ' (Dry Run)' : ''}
                </span>
              </div>
              <span className="text-xs text-slate-400">
                {progress.processed} / {progress.total} tracks
              </span>
            </div>
            <ProgressBar
              value={progress.total > 0 ? (progress.processed / progress.total) * 100 : 0}
            />
            {progress.current_file && (
              <div className="text-xs text-slate-500 truncate">{progress.current_file}</div>
            )}
            <div className="flex gap-4 text-xs text-slate-400">
              <span>Matched: {progress.matched}</span>
              <span>Auto: {progress.auto_approved}</span>
              <span>Flagged: {progress.flagged}</span>
              <span>Unmatched: {progress.unmatched}</span>
              <span>Failed: {progress.failed}</span>
              <span className="ml-auto">{Math.floor(progress.elapsed_s / 60)}m {progress.elapsed_s % 60}s</span>
            </div>
          </div>
        </GlassCard>
      )}

      {/* Stats Grid */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4">
          <GlassCard className="!p-4">
            <StatCard label="Total Tracks" value={stats.total_tracks.toLocaleString()} />
          </GlassCard>
          <GlassCard className="!p-4">
            <StatCard label="Processed" value={stats.processed.toLocaleString()} sub={`${stats.unprocessed} remaining`} />
          </GlassCard>
          <GlassCard className="!p-4">
            <StatCard label="Matched" value={stats.matched.toLocaleString()} />
          </GlassCard>
          <GlassCard className="!p-4">
            <StatCard label="Flagged" value={stats.flagged.toLocaleString()} />
          </GlassCard>
          <GlassCard className="!p-4">
            <StatCard label="Unmatched" value={stats.unmatched.toLocaleString()} />
          </GlassCard>
          <GlassCard className="!p-4">
            <StatCard label="Failed" value={stats.failed.toLocaleString()} />
          </GlassCard>
        </div>
      )}

      {/* Quick Navigation Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="cursor-pointer" onClick={() => navigate('/fingerprint/review')}>
          <GlassCard className="hover:border-[#d4a017]/40 transition-colors">
            <div className="flex items-center gap-3">
              <Eye className="w-5 h-5 text-amber-400" />
              <div>
                <div className="text-sm font-medium text-white">Review Queue</div>
                <div className="text-xs text-slate-400">{stats?.flagged ?? 0} tracks awaiting review</div>
              </div>
            </div>
          </GlassCard>
        </div>

        <div className="cursor-pointer" onClick={() => navigate('/fingerprint/unmatched')}>
          <GlassCard className="hover:border-[#d4a017]/40 transition-colors">
            <div className="flex items-center gap-3">
              <AlertCircle className="w-5 h-5 text-orange-400" />
              <div>
                <div className="text-sm font-medium text-white">Unmatched</div>
                <div className="text-xs text-slate-400">{stats?.unmatched ?? 0} tracks with no match</div>
              </div>
            </div>
          </GlassCard>
        </div>

        <div className="cursor-pointer" onClick={() => navigate('/fingerprint/history')}>
          <GlassCard className="hover:border-[#d4a017]/40 transition-colors">
            <div className="flex items-center gap-3">
              <History className="w-5 h-5 text-blue-400" />
              <div>
                <div className="text-sm font-medium text-white">Change History</div>
                <div className="text-xs text-slate-400">View & rollback tag changes</div>
              </div>
            </div>
          </GlassCard>
        </div>
      </div>

      {/* AudD Budget + MB Mirror Status */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {stats?.audd && (
          <GlassCard>
            <h3 className="text-sm font-medium text-white mb-3">AudD Budget</h3>
            <div className="space-y-2">
              <div className="flex justify-between text-xs">
                <span className="text-slate-400">This month</span>
                <span className="text-white">${stats.audd.month_cost_dollars} / ${stats.audd.budget_dollars}</span>
              </div>
              <ProgressBar
                value={(stats.audd.month_cost_dollars / stats.audd.budget_dollars) * 100}
              />
              <div className="flex justify-between text-xs text-slate-500">
                <span>{stats.audd.month_requests} requests</span>
                <span>${stats.audd.budget_remaining_dollars} remaining</span>
              </div>
            </div>
          </GlassCard>
        )}

        <GlassCard>
          <h3 className="text-sm font-medium text-white mb-3">MusicBrainz Mirror</h3>
          <div className="flex items-center gap-2">
            <Database className="w-4 h-4 text-slate-400" />
            {mbAvailable ? (
              <Badge variant="green">Local Mirror Active</Badge>
            ) : (
              <Badge variant="amber">Using Public API</Badge>
            )}
          </div>
          <p className="text-xs text-slate-500 mt-2">
            {mbAvailable
              ? 'Unlimited local lookups via PostgreSQL mirror'
              : 'Rate limited to 1 req/sec — set up mbslave for unlimited access'}
          </p>
        </GlassCard>
      </div>

      {/* Genre Distribution */}
      {stats?.genre_distribution && stats.genre_distribution.length > 0 && (
        <GlassCard>
          <h3 className="text-sm font-medium text-white mb-3">Genre Distribution</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-2">
            {stats.genre_distribution.slice(0, 12).map((g) => (
              <div key={g.matched_genre} className="flex justify-between text-xs px-2 py-1 bg-slate-800/50 rounded">
                <span className="text-slate-300 truncate">{g.matched_genre}</span>
                <span className="text-slate-500 ml-1">{g.count}</span>
              </div>
            ))}
          </div>
        </GlassCard>
      )}

      {/* Match Source Distribution */}
      {stats?.source_counts && Object.keys(stats.source_counts).length > 0 && (
        <GlassCard>
          <h3 className="text-sm font-medium text-white mb-3">Match Sources</h3>
          <div className="flex gap-4">
            {Object.entries(stats.source_counts).map(([source, count]) => (
              <div key={source} className="flex items-center gap-2">
                <CheckCircle className="w-3.5 h-3.5 text-emerald-400" />
                <span className="text-xs text-slate-300 capitalize">{source}</span>
                <span className="text-xs text-slate-500">{count}</span>
              </div>
            ))}
          </div>
        </GlassCard>
      )}
    </div>
  )
}
