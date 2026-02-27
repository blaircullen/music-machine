import { useState, useEffect, useCallback, useRef } from 'react'
import { RefreshCw, Trash2, AlertTriangle, CheckCircle, Loader2, Clock } from 'lucide-react'
import toast from 'react-hot-toast'
import { Button } from '../components/ui/Button'
import { Modal } from '../components/ui/Modal'
import { StatusBadge } from '../components/ui/Badge'
import {
  getJobs,
  retryJob,
  getTrash,
  getTrashStats,
  emptyTrash,
  restoreTrashItem,
  type Job,
  type TrashItem,
  type TrashStats,
} from '../lib/api'

type JobFilter = 'all' | 'running' | 'failed' | 'completed'

const JOB_FILTERS: { key: JobFilter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'running', label: 'Running' },
  { key: 'failed', label: 'Failed' },
  { key: 'completed', label: 'Completed' },
]

// SQLite CURRENT_TIMESTAMP is UTC but has no Z suffix — append it so JS parses correctly
function toUtcDate(iso: string): Date {
  return new Date(iso.endsWith('Z') ? iso : iso + 'Z')
}

function formatDuration(start: string, end: string): string {
  const ms = toUtcDate(end).getTime() - toUtcDate(start).getTime()
  if (isNaN(ms) || ms < 0) return '—'
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return `${m}m ${s % 60}s`
}

function formatDateShort(iso: string): string {
  try {
    return toUtcDate(iso).toLocaleString('en-US', {
      timeZone: 'America/New_York',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function fmtBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

// ─── Job type label ───────────────────────────────────────────────────────────

const JOB_TYPE_LABELS: Record<string, string> = {
  scan: 'Library Scan',
  analyze: 'Dupe Analysis',
  upgrade_search: 'Upgrade Search',
  upgrade_download: 'Download FLAC',
  resolve: 'Resolve Dupes',
}

function jobTypeLabel(type: string): string {
  return JOB_TYPE_LABELS[type] ?? type.replace(/_/g, ' ')
}

// ─── JobLog component ─────────────────────────────────────────────────────────

export default function JobLog() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [loadingJobs, setLoadingJobs] = useState(true)
  const [filter, setFilter] = useState<JobFilter>('all')
  const [retrying, setRetrying] = useState<Set<number>>(new Set())

  const [trash, setTrash] = useState<TrashItem[]>([])
  const [trashStats, setTrashStats] = useState<TrashStats | null>(null)
  const [loadingTrash, setLoadingTrash] = useState(true)
  const [restoring, setRestoring] = useState<Set<number>>(new Set())
  const [showEmptyModal, setShowEmptyModal] = useState(false)

  const autoRefreshRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const mountedRef = useRef(true)

  const fetchJobs = useCallback(async () => {
    try {
      const data = await getJobs()
      if (mountedRef.current) setJobs(data)
    } catch {
      // silently handle — avoid spamming toasts on auto-refresh
    } finally {
      if (mountedRef.current) setLoadingJobs(false)
    }
  }, [])

  const fetchTrash = useCallback(async () => {
    try {
      const [items, stats] = await Promise.all([getTrash(), getTrashStats()])
      if (mountedRef.current) {
        setTrash(items)
        setTrashStats(stats)
      }
    } catch {
      // silently ignore
    } finally {
      if (mountedRef.current) setLoadingTrash(false)
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    fetchJobs()
    fetchTrash()

    autoRefreshRef.current = setInterval(() => {
      fetchJobs()
    }, 5000)

    return () => {
      mountedRef.current = false
      if (autoRefreshRef.current) clearInterval(autoRefreshRef.current)
    }
  }, [fetchJobs, fetchTrash])

  const handleRetry = async (id: number) => {
    setRetrying((prev) => new Set(prev).add(id))
    try {
      await retryJob(id)
      toast.success('Job retried')
      await fetchJobs()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to retry job')
    } finally {
      setRetrying((prev) => { const next = new Set(prev); next.delete(id); return next })
    }
  }

  const handleRestore = async (id: number, path: string) => {
    setRestoring((prev) => new Set(prev).add(id))
    try {
      await restoreTrashItem(id)
      setTrash((prev) => prev.filter((t) => t.id !== id))
      toast.success(`Restored: ${path.split('/').pop() ?? path}`)
      fetchTrash()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to restore')
    } finally {
      setRestoring((prev) => { const next = new Set(prev); next.delete(id); return next })
    }
  }

  const handleEmptyTrash = async () => {
    setShowEmptyModal(false)
    try {
      const { deleted } = await emptyTrash()
      toast.success(`Deleted ${deleted} file${deleted !== 1 ? 's' : ''} permanently`)
      setTrash([])
      setTrashStats({ count: 0, size_bytes: 0 })
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to empty trash')
    }
  }

  const filtered = jobs.filter((j) => filter === 'all' || j.status === filter)

  const statusIcon = (status: string) => {
    if (status === 'running') return <Loader2 className="w-3.5 h-3.5 text-[#60a5fa] animate-spin" />
    if (status === 'completed') return <CheckCircle className="w-3.5 h-3.5 text-[#4ade80]" />
    if (status === 'failed') return <AlertTriangle className="w-3.5 h-3.5 text-[#f87171]" />
    return <Clock className="w-3.5 h-3.5 text-slate-500" />
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-white">Job Log</h1>
          <p className="text-xs text-slate-500 mt-0.5">Auto-refreshes every 5 seconds</p>
        </div>
        <Button variant="secondary" size="sm" onClick={fetchJobs}>
          <RefreshCw className="w-3.5 h-3.5" />
          Refresh
        </Button>
      </div>

      {/* Filter tabs */}
      <div className="flex gap-1">
        {JOB_FILTERS.map(({ key, label }) => {
          const count = key === 'all' ? jobs.length : jobs.filter((j) => j.status === key).length
          return (
            <button
              key={key}
              onClick={() => setFilter(key)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                filter === key
                  ? 'bg-[#6c63ff]/15 text-[#a89fff] border border-[#6c63ff]/30'
                  : 'text-slate-500 hover:text-slate-300 hover:bg-[#2a2d3a]'
              }`}
            >
              {label}
              {count > 0 && (
                <span className="ml-1.5 text-slate-600 tabular-nums">{count}</span>
              )}
            </button>
          )
        })}
      </div>

      {/* Jobs table */}
      {loadingJobs ? (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-14 rounded-lg bg-[#1a1d27] border border-[#2a2d3a] animate-pulse" />
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-xl bg-[#1a1d27] border border-[#2a2d3a] p-12 text-center">
          <ScrollText className="w-8 h-8 text-slate-600 mx-auto mb-3" />
          <p className="text-slate-400 text-sm">No jobs found</p>
        </div>
      ) : (
        <div className="rounded-xl bg-[#1a1d27] border border-[#2a2d3a] overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-[#2a2d3a] text-xs text-slate-500 uppercase tracking-wide">
                <th className="px-4 py-3 text-left font-medium">Type</th>
                <th className="px-4 py-3 text-center font-medium">Status</th>
                <th className="px-4 py-3 text-left font-medium">Started</th>
                <th className="px-4 py-3 text-left font-medium">Duration</th>
                <th className="px-4 py-3 text-left font-medium">Error</th>
                <th className="px-4 py-3 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((job) => {
                const isRetrying = retrying.has(job.id)
                return (
                  <tr
                    key={job.id}
                    className="border-b border-[#2a2d3a]/50 hover:bg-white/[0.02] transition-colors"
                  >
                    <td className="px-4 py-3.5">
                      <div className="flex items-center gap-2">
                        {statusIcon(job.status)}
                        <span className="text-slate-200 font-medium text-xs">
                          {jobTypeLabel(job.job_type)}
                        </span>
                      </div>
                    </td>
                    <td className="px-4 py-3.5 text-center">
                      <StatusBadge status={job.status} />
                    </td>
                    <td className="px-4 py-3.5 text-slate-400 text-xs tabular-nums">
                      {formatDateShort(job.created_at)}
                    </td>
                    <td className="px-4 py-3.5 text-slate-400 text-xs tabular-nums">
                      {formatDuration(job.created_at, job.updated_at)}
                    </td>
                    <td className="px-4 py-3.5 max-w-xs">
                      {job.error_msg && (
                        <span
                          className="text-xs text-[#f87171] truncate block"
                          title={job.error_msg}
                        >
                          {job.error_msg}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3.5 text-right">
                      {job.status === 'failed' && (
                        <Button
                          variant="secondary"
                          size="sm"
                          loading={isRetrying}
                          disabled={isRetrying}
                          onClick={() => handleRetry(job.id)}
                        >
                          <RefreshCw className="w-3.5 h-3.5" />
                          Retry
                        </Button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* ── Trash section ── */}
      <div className="rounded-xl bg-[#1a1d27] border border-[#2a2d3a] overflow-hidden">
        <div className="flex items-center justify-between px-5 py-4 border-b border-[#2a2d3a]">
          <div className="flex items-center gap-3">
            <Trash2 className="w-4 h-4 text-slate-500" />
            <div>
              <h2 className="text-sm font-semibold text-slate-200">Trash</h2>
              {trashStats && (
                <p className="text-xs text-slate-500 mt-0.5">
                  {trashStats.count} file{trashStats.count !== 1 ? 's' : ''} &middot; {fmtBytes(trashStats.size_bytes)}
                </p>
              )}
            </div>
          </div>
          <Button
            variant="danger"
            size="sm"
            onClick={() => setShowEmptyModal(true)}
            disabled={!trashStats || trashStats.count === 0}
          >
            <Trash2 className="w-3.5 h-3.5" />
            Empty Trash
          </Button>
        </div>

        <Modal
          open={showEmptyModal}
          onClose={() => setShowEmptyModal(false)}
          title="Empty Trash"
          message={`This will permanently delete ${trashStats?.count ?? 0} file${(trashStats?.count ?? 0) !== 1 ? 's' : ''} (${fmtBytes(trashStats?.size_bytes ?? 0)}). This cannot be undone.`}
          confirmLabel="Delete Permanently"
          confirmVariant="danger"
          onConfirm={handleEmptyTrash}
        />

        {loadingTrash ? (
          <div className="p-8 text-center text-slate-600 text-sm">Loading...</div>
        ) : trash.length === 0 ? (
          <div className="p-8 text-center">
            <p className="text-slate-500 text-sm">Trash is empty</p>
            <p className="text-slate-600 text-xs mt-1">
              Resolved duplicates will appear here until permanently deleted
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-[#2a2d3a] text-xs text-slate-500 uppercase tracking-wide">
                  <th className="px-4 py-3 text-left font-medium">Original Path</th>
                  <th className="px-4 py-3 text-left font-medium">Size</th>
                  <th className="px-4 py-3 text-left font-medium">Moved</th>
                  <th className="px-4 py-3 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {trash.map((item) => {
                  const isRestoring = restoring.has(item.id)
                  const filename = item.original_path.split('/').pop() ?? item.original_path
                  return (
                    <tr
                      key={item.id}
                      className="border-b border-[#2a2d3a]/50 hover:bg-white/[0.02] transition-colors"
                    >
                      <td className="px-4 py-3.5 max-w-sm">
                        <span
                          className="text-xs font-mono text-slate-400 truncate block"
                          title={item.original_path}
                        >
                          {filename}
                        </span>
                        <span
                          className="text-xs font-mono text-slate-600 truncate block mt-0.5"
                          title={item.original_path}
                        >
                          {item.original_path}
                        </span>
                      </td>
                      <td className="px-4 py-3.5 text-slate-400 text-xs tabular-nums">
                        {fmtBytes(item.file_size)}
                      </td>
                      <td className="px-4 py-3.5 text-slate-400 text-xs">
                        {formatDateShort(item.moved_at)}
                      </td>
                      <td className="px-4 py-3.5 text-right">
                        <Button
                          variant="secondary"
                          size="sm"
                          loading={isRestoring}
                          disabled={isRestoring}
                          onClick={() => handleRestore(item.id, item.original_path)}
                        >
                          Restore
                        </Button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

// Inline icon to avoid import collision
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
