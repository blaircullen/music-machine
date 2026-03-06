import { useState, useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import { motion, AnimatePresence } from 'motion/react'
import {
  Music2,
  HardDrive,
  Copy,
  ArrowUpCircle,
  Sparkles,
  Scan,
  ChevronRight,
  Clock,
  FileAudio,
  FolderSync,
  AlertCircle,
  CheckCircle2,
  X,
} from 'lucide-react'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import toast from 'react-hot-toast'
import { StatCard } from '../components/ui/StatCard'
import { ProgressBar } from '../components/ui/ProgressBar'
import { Button } from '../components/ui/Button'
import { useStats } from '../hooks/useStats'
import { useWebSocket } from '../hooks/useWebSocket'
import { postScan, getScanStatus, type ScanStatus } from '../lib/api'
import { useReorgStatus } from '../hooks/useReorgStatus'

const FORMAT_COLORS: Record<string, string> = {
  flac: '#22c55e',
  mp3: '#f97316',
  aac: '#eab308',
  alac: '#34d399',
  wav: '#3b82f6',
  aiff: '#60a5fa',
  ogg: '#f59e0b',
  wma: '#6b7280',
  m4a: '#facc15',
}

function formatElapsed(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  if (m === 0) return `${s}s`
  return `${m}m ${s}s`
}

function truncatePath(path: string, maxLen = 60): string {
  if (path.length <= maxLen) return path
  return '...' + path.slice(-(maxLen - 3))
}

interface ScanProgressState {
  running: boolean
  phase: string
  progress: number
  total: number
  current_file: string
  elapsed_s: number
}

const INITIAL_SCAN: ScanProgressState = {
  running: false,
  phase: '',
  progress: 0,
  total: 0,
  current_file: '',
  elapsed_s: 0,
}

const PHASE_LABELS: Record<string, string> = {
  counting: 'Counting files…',
  scanning: 'Reading audio tags…',
  cleaning: 'Removing stale records…',
  analyzing: 'Hunting duplicates…',
  complete: 'Scan complete',
}

const REORG_PHASE_LABELS: Record<string, string> = {
  scanning: 'Scanning library...',
  inbox: 'Pulling from inbox...',
  cleaning: 'Cleaning empty dirs...',
  complete: 'Reorg complete',
  failed: 'Reorg failed',
}

function formatRelativeTime(isoStr: string): string {
  const diff = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000)
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return new Date(isoStr).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export default function Dashboard() {
  const { stats, loading: statsLoading, refetch } = useStats()
  const { lastMessage, isConnected } = useWebSocket()
  const [scanState, setScanState] = useState<ScanProgressState>(INITIAL_SCAN)
  const [scanResult, setScanResult] = useState<{ elapsed_s: number } | null>(null)
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const wasRunningRef = useRef(false)
  const scanResultTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reorgStatus = useReorgStatus()

  useEffect(() => {
    return () => { if (scanResultTimerRef.current) clearTimeout(scanResultTimerRef.current) }
  }, [])

  // Merge WebSocket scan_progress events into local state
  useEffect(() => {
    if (!lastMessage) return
    if (lastMessage.type === 'scan_progress') {
      const d = lastMessage.data as Partial<ScanStatus> & { running?: boolean }
      setScanState((prev) => ({
        ...prev,
        running: d.running ?? prev.running,
        phase: d.phase ?? prev.phase,
        progress: d.progress ?? prev.progress,
        total: d.total ?? prev.total,
        current_file: d.current_file ?? prev.current_file,
        elapsed_s: d.elapsed_s ?? prev.elapsed_s,
      }))
    }
  }, [lastMessage])

  // Fallback polling when WebSocket is disconnected
  useEffect(() => {
    if (isConnected) {
      if (pollTimerRef.current) { clearInterval(pollTimerRef.current); pollTimerRef.current = null }
      return
    }

    const poll = () => {
      getScanStatus()
        .then((data) => setScanState(data))
        .catch(() => {})
    }
    poll()
    pollTimerRef.current = setInterval(poll, 3000)
    return () => { if (pollTimerRef.current) clearInterval(pollTimerRef.current) }
  }, [isConnected])

  // Refresh stats + show celebration when scan finishes
  useEffect(() => {
    if (wasRunningRef.current && !scanState.running) {
      refetch()
      if (scanState.phase === 'complete' || scanState.phase === '') {
        setScanResult({ elapsed_s: scanState.elapsed_s })
        if (scanResultTimerRef.current) clearTimeout(scanResultTimerRef.current)
        scanResultTimerRef.current = setTimeout(() => setScanResult(null), 7000)
      }
    }
    wasRunningRef.current = scanState.running
  }, [scanState.running, scanState.phase, scanState.elapsed_s, refetch])

  const handleReorg = async () => {
    try {
      const res = await fetch('/api/reorg/start', { method: 'POST' })
      const data = await res.json()
      if (!data.ok) throw new Error(data.error ?? 'Failed')
      toast.success('Library reorg started')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to start reorg')
    }
  }

  const handleScan = async () => {
    try {
      await postScan()
      toast.success('Scan started')
      setScanState((prev) => ({ ...prev, running: true, phase: 'counting', progress: 0, total: 0, current_file: '' }))
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to start scan')
    }
  }

  const flacPct =
    stats && stats.total_tracks > 0
      ? Math.round((stats.flac_count / stats.total_tracks) * 100)
      : 0

  const chartData = stats?.formats
    .filter((f) => f.count > 0)
    .sort((a, b) => b.count - a.count) ?? []

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-white">Dashboard</h1>
          <p className="text-xs text-slate-500 mt-0.5">Library health at a glance</p>
        </div>
        <Button
          variant="primary"
          onClick={handleScan}
          disabled={scanState.running}
          loading={scanState.running}
        >
          <Scan className="w-4 h-4" />
          {scanState.running ? 'Scanning...' : 'Scan Library'}
        </Button>
      </div>

      {/* Active scan panel */}
      {scanState.running && (
        <div className="rounded-xl bg-[#1a1d27] border border-[#d4a017]/30 p-5 shadow-[0_0_20px_rgba(212,160,23,0.1)]">
          <div className="flex items-center gap-2 mb-3">
            <div className="w-2 h-2 rounded-full bg-[#d4a017] animate-pulse" />
            <span className="text-sm font-medium text-[#f0c95c]">
              {PHASE_LABELS[scanState.phase] ?? scanState.phase ?? 'Working...'}
            </span>
          </div>

          <ProgressBar
            value={scanState.total > 0 ? scanState.progress : undefined}
            max={scanState.total > 0 ? scanState.total : undefined}
            active
          />

          <div className="flex items-center justify-between mt-3 gap-4">
            {scanState.current_file ? (
              <div className="flex items-center gap-1.5 min-w-0">
                <FileAudio className="w-3.5 h-3.5 text-slate-500 shrink-0" />
                <span className="text-xs text-slate-500 font-mono truncate">
                  {truncatePath(scanState.current_file)}
                </span>
              </div>
            ) : (
              <span />
            )}
            {scanState.elapsed_s > 0 && (
              <div className="flex items-center gap-1.5 shrink-0">
                <Clock className="w-3.5 h-3.5 text-slate-500" />
                <span className="text-xs text-slate-500 font-mono">
                  {formatElapsed(scanState.elapsed_s)}
                </span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Scan complete celebration */}
      <AnimatePresence>
        {scanResult && !scanState.running && (
          <motion.div
            initial={{ opacity: 0, scale: 0.98, y: -6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: -4 }}
            transition={{ type: 'spring', stiffness: 400, damping: 30 }}
            className="rounded-xl bg-[#22c55e]/10 border border-[#22c55e]/30 p-4 shadow-[0_0_24px_rgba(34,197,94,0.08)] flex items-center justify-between"
          >
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-lg bg-[#22c55e]/20 flex items-center justify-center shrink-0">
                <CheckCircle2 className="w-4 h-4 text-[#4ade80]" />
              </div>
              <div>
                <p className="text-sm font-semibold text-[#4ade80]">Scan complete</p>
                <p className="text-xs text-slate-500">
                  {scanResult.elapsed_s > 0
                    ? `Finished in ${formatElapsed(scanResult.elapsed_s)}. `
                    : ''}
                  {stats && stats.dupes_found > 0
                    ? `Caught ${stats.dupes_found} duplicate${stats.dupes_found === 1 ? '' : 's'}.`
                    : stats
                    ? 'Library looks clean.'
                    : 'Stats updated below.'}
                </p>
              </div>
            </div>
            <button
              onClick={() => setScanResult(null)}
              aria-label="Dismiss scan result"
              className="text-slate-600 hover:text-slate-400 p-1 transition-colors rounded"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Stat cards */}
      {statsLoading ? (
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-24 rounded-xl bg-[#1a1d27] border border-[#2a2d3a] animate-pulse" />
          ))}
        </div>
      ) : stats ? (
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
          <StatCard
            icon={Music2}
            label="Total Tracks"
            value={stats.total_tracks.toLocaleString()}
            accent="purple"
          />
          <StatCard
            icon={Sparkles}
            label="FLAC"
            value={stats.flac_count.toLocaleString()}
            subtitle={`${flacPct}% of library`}
            accent="green"
          />
          <StatCard
            icon={ArrowUpCircle}
            label="Needs Upgrade"
            value={stats.lossy_count.toLocaleString()}
            accent="amber"
          />
          <StatCard
            icon={Copy}
            label="Duplicates Found"
            value={stats.dupes_found.toLocaleString()}
            accent="red"
          />
          <StatCard
            icon={HardDrive}
            label="Library Size"
            value={`${stats.library_size_gb.toFixed(2)} GB`}
            accent="blue"
          />
        </div>
      ) : null}

      {/* Format breakdown + quick actions */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Bar chart */}
        <div className="lg:col-span-2 rounded-xl bg-[#1a1d27] border border-[#2a2d3a] p-5">
          <h2 className="text-sm font-semibold text-slate-300 mb-4">Format Breakdown</h2>
          {chartData.length === 0 ? (
            <div className="flex items-center justify-center h-40 text-slate-600 text-sm">
              No data — run a scan first
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={chartData} layout="vertical" margin={{ left: 8, right: 16, top: 0, bottom: 0 }}>
                <XAxis type="number" hide />
                <YAxis
                  dataKey="format"
                  type="category"
                  width={40}
                  tick={{ fill: '#94a3b8', fontSize: 11, fontFamily: 'monospace' }}
                  tickFormatter={(v: string) => v.toUpperCase()}
                  axisLine={false}
                  tickLine={false}
                />
                <Tooltip
                  cursor={{ fill: 'rgba(255,255,255,0.03)' }}
                  contentStyle={{
                    background: '#13151f',
                    border: '1px solid #2a2d3a',
                    borderRadius: 8,
                    color: '#e2e8f0',
                    fontSize: 12,
                  }}
                />
                <Bar dataKey="count" radius={[0, 4, 4, 0]} maxBarSize={20}>
                  {chartData.map((entry, i) => (
                    <Cell
                      key={i}
                      fill={FORMAT_COLORS[entry.format.toLowerCase()] ?? '#d4a017'}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>

        {/* Quick actions */}
        <div className="rounded-xl bg-[#1a1d27] border border-[#2a2d3a] p-5">
          <h2 className="text-sm font-semibold text-slate-300 mb-4">Quick Actions</h2>
          <div className="space-y-2">
            {stats && stats.dupes_found > 0 && (
              <Link
                to="/library?tab=duplicates"
                className="flex items-center justify-between w-full rounded-lg p-3 bg-[#ef4444]/10 border border-[#ef4444]/20 hover:bg-[#ef4444]/15 transition-colors group"
              >
                <div className="flex items-center gap-2.5">
                  <Copy className="w-4 h-4 text-[#f87171]" />
                  <div>
                    <p className="text-sm font-medium text-[#f87171]">
                      {stats.dupes_found} Duplicates
                    </p>
                    <p className="text-xs text-slate-500">Review and resolve</p>
                  </div>
                </div>
                <ChevronRight className="w-4 h-4 text-slate-600 group-hover:text-[#f87171] transition-colors" />
              </Link>
            )}

            {stats && stats.lossy_upgrades_pending > 0 && (
              <Link
                to="/upgrades"
                className="flex items-center justify-between w-full rounded-lg p-3 bg-[#ef4444]/10 border border-[#ef4444]/20 hover:bg-[#ef4444]/15 transition-colors group"
              >
                <div className="flex items-center gap-2.5">
                  <ArrowUpCircle className="w-4 h-4 text-[#f87171]" />
                  <div>
                    <p className="text-sm font-medium text-[#f87171]">
                      {stats.lossy_upgrades_pending.toLocaleString()} Lossy Tracks
                    </p>
                    <p className="text-xs text-slate-500">Need lossless replacement</p>
                  </div>
                </div>
                <ChevronRight className="w-4 h-4 text-slate-600 group-hover:text-[#f87171] transition-colors" />
              </Link>
            )}

            {stats && stats.hires_upgrades_pending > 0 && (
              <Link
                to="/upgrades"
                className="flex items-center justify-between w-full rounded-lg p-3 bg-[#d4a017]/10 border border-[#d4a017]/20 hover:bg-[#d4a017]/15 transition-colors group"
              >
                <div className="flex items-center gap-2.5">
                  <Sparkles className="w-4 h-4 text-[#f0c95c]" />
                  <div>
                    <p className="text-sm font-medium text-[#f0c95c]">
                      {stats.hires_upgrades_pending.toLocaleString()} Hi-Res Queued
                    </p>
                    <p className="text-xs text-slate-500">FLAC → Hi-Res upgrades pending</p>
                  </div>
                </div>
                <ChevronRight className="w-4 h-4 text-slate-600 group-hover:text-[#f0c95c] transition-colors" />
              </Link>
            )}

            {(!stats || (stats.dupes_found === 0 && stats.lossy_upgrades_pending === 0)) && (
              <div className="flex items-center gap-2.5 p-3 rounded-lg bg-[#22c55e]/10 border border-[#22c55e]/20">
                <Sparkles className="w-4 h-4 text-[#4ade80] shrink-0" />
                <div>
                  <p className="text-sm text-[#4ade80]">Library looks clean</p>
                  {stats && stats.total_tracks > 0 && (
                    <p className="text-xs text-slate-500 mt-0.5">
                      {flacPct}% lossless — no action needed
                    </p>
                  )}
                </div>
              </div>
            )}

            <Link
              to="/jobs"
              className="flex items-center justify-between w-full rounded-lg p-3 bg-[#2a2d3a] hover:bg-[#3a3d4a] transition-colors group"
            >
              <div className="flex items-center gap-2.5">
                <ScrollText className="w-4 h-4 text-slate-400" />
                <p className="text-sm text-slate-400 group-hover:text-slate-200 transition-colors">
                  View Job Log
                </p>
              </div>
              <ChevronRight className="w-4 h-4 text-slate-600 group-hover:text-slate-400 transition-colors" />
            </Link>
          </div>
        </div>
      </div>
      {/* Library Reorg panel */}
      <div className={`rounded-xl bg-[#1a1d27] border p-5 transition-colors duration-300 ${
        reorgStatus.running ? 'border-[#d4a017]/30 shadow-[0_0_20px_rgba(212,160,23,0.08)]' : 'border-[#2a2d3a]'
      }`}>
        {/* Header row */}
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <div className={`shrink-0 rounded-lg p-2.5 ${reorgStatus.running ? 'bg-[#d4a017]/15 text-[#e6b422]' : 'bg-[#d4a017]/10 text-[#f0c95c]'}`}>
              <FolderSync className={`w-5 h-5 ${reorgStatus.running ? 'animate-spin' : ''}`} style={reorgStatus.running ? { animationDuration: '3s' } : {}} />
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <p className="text-sm font-semibold text-slate-200">Library Reorg</p>
                {reorgStatus.running && (
                  <span className="text-xs text-[#f0c95c] font-medium">
                    {REORG_PHASE_LABELS[reorgStatus.phase] ?? reorgStatus.phase ?? 'Working...'}
                  </span>
                )}
              </div>
              {!reorgStatus.running && (
                reorgStatus.last_run ? (
                  reorgStatus.last_run.error ? (
                    <div className="flex items-center gap-1.5 mt-0.5">
                      <AlertCircle className="w-3 h-3 text-[#f87171] shrink-0" />
                      <span className="text-xs text-[#f87171]">{reorgStatus.last_run.error}</span>
                    </div>
                  ) : (
                    <p className="text-xs text-slate-500 mt-0.5">
                      {formatRelativeTime(reorgStatus.last_run.timestamp)}
                      {' · '}
                      <span className="text-slate-400">{reorgStatus.last_run.moved} moved</span>
                      {reorgStatus.last_run.inbox_moved > 0 && (
                        <> · <span className="text-[#f0c95c]">{reorgStatus.last_run.inbox_moved} from inbox</span></>
                      )}
                      {' · '}
                      {reorgStatus.last_run.skipped} skipped
                      {reorgStatus.last_run.errors > 0 && (
                        <> · <span className="text-[#f87171]">{reorgStatus.last_run.errors} errors</span></>
                      )}
                    </p>
                  )
                ) : (
                  <p className="text-xs text-slate-500 mt-0.5">No runs recorded yet</p>
                )
              )}
            </div>
          </div>
          <div className="flex items-center gap-3 shrink-0">
            {reorgStatus.running && reorgStatus.elapsed_s > 0 && (
              <span className="text-xs text-slate-500 font-mono flex items-center gap-1">
                <Clock className="w-3 h-3" />
                {formatElapsed(reorgStatus.elapsed_s)}
              </span>
            )}
            <Button
              variant="secondary"
              size="sm"
              onClick={handleReorg}
              disabled={reorgStatus.running}
              loading={reorgStatus.running}
            >
              <FolderSync className="w-3.5 h-3.5" />
              {reorgStatus.running ? 'Running...' : 'Run Now'}
            </Button>
          </div>
        </div>

        {/* Live progress — only while running */}
        {reorgStatus.running && (
          <div className="mt-4 space-y-3">
            {/* Progress bar */}
            <ProgressBar
              value={reorgStatus.total > 0 ? reorgStatus.progress : undefined}
              max={reorgStatus.total > 0 ? reorgStatus.total : undefined}
              active
            />

            {/* Live counters */}
            <div className="flex flex-wrap gap-3 text-xs">
              <span className="flex items-center gap-1 text-[#4ade80]">
                <span className="font-bold tabular-nums">{reorgStatus.moved}</span>
                <span className="text-slate-500">moved</span>
              </span>
              {reorgStatus.inbox_moved > 0 && (
                <span className="flex items-center gap-1 text-[#f0c95c]">
                  <span className="font-bold tabular-nums">{reorgStatus.inbox_moved}</span>
                  <span className="text-slate-500">from inbox</span>
                </span>
              )}
              <span className="flex items-center gap-1 text-slate-400">
                <span className="font-bold tabular-nums">{reorgStatus.already_ok}</span>
                <span className="text-slate-500">already ok</span>
              </span>
              <span className="flex items-center gap-1 text-slate-400">
                <span className="font-bold tabular-nums">{reorgStatus.skipped}</span>
                <span className="text-slate-500">skipped</span>
              </span>
              {reorgStatus.errors > 0 && (
                <span className="flex items-center gap-1 text-[#f87171]">
                  <span className="font-bold tabular-nums">{reorgStatus.errors}</span>
                  <span className="text-slate-500">errors</span>
                </span>
              )}
              {reorgStatus.total > 0 && (
                <span className="ml-auto text-slate-600 tabular-nums">
                  {reorgStatus.progress} / {reorgStatus.total}
                </span>
              )}
            </div>

            {/* Current file */}
            {reorgStatus.current_file && (
              <div className="flex items-center gap-1.5 min-w-0">
                <FileAudio className="w-3.5 h-3.5 text-slate-600 shrink-0" />
                <span className="text-xs text-slate-600 font-mono truncate">
                  {truncatePath(reorgStatus.current_file)}
                </span>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// Local import to avoid circular — inline the icon
function ScrollText(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <path d="M8 21h12a2 2 0 0 0 2-2v-2H10v2a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v3h4" />
      <path d="M19 17V5a2 2 0 0 0-2-2H4" />
      <path d="M15 8h-5" />
      <path d="M15 12h-5" />
    </svg>
  )
}
