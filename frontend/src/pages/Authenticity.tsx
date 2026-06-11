import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  FileAudio,
  RefreshCw,
  Search,
  ShieldCheck,
  Waves,
  XCircle,
} from 'lucide-react'
import {
  Badge,
  Button,
  EmptyState,
  GlassCard,
  Modal,
  Skeleton,
  SkeletonTable,
  StatCard,
  toast,
} from '../components/ui'

type Verdict = 'lossless' | 'suspect' | 'transcode' | 'insufficient_audio' | 'low_rate' | string
type FilterValue = 'all' | 'transcode' | 'suspect' | 'lossless' | 'insufficient_audio'

interface AuthenticitySummary {
  counts: Record<string, number>
  analyzed: number
  total_flac: number
  coverage_pct: number
  recue?: {
    triggered: number
    fixed: number
    staged: number
    by_source: Record<string, number>
  }
}

interface AuthenticityItem {
  track_id: number
  verdict: Verdict
  confidence: number | null
  cutoff_hz: number | null
  source_guess: string | null
  sample_rate: number | null
  artist: string | null
  title: string | null
  album: string | null
  file_path: string
}

interface AuthenticityListResponse {
  total: number
  items: AuthenticityItem[]
}

interface IngestResponse {
  ingested: number
  matched: number
  unmatched: number
}

const PAGE_SIZE = 100

const FILTERS: Array<{ label: string; value: FilterValue }> = [
  { label: 'All', value: 'all' },
  { label: 'Transcode', value: 'transcode' },
  { label: 'Suspect', value: 'suspect' },
  { label: 'Lossless', value: 'lossless' },
  { label: 'Insufficient', value: 'insufficient_audio' },
]

const VERDICT_BADGE: Record<string, 'green' | 'amber' | 'red' | 'gray'> = {
  lossless: 'green',
  suspect: 'amber',
  transcode: 'red',
  low_rate: 'gray',
  insufficient_audio: 'gray',
}

function verdictVariant(verdict: Verdict) {
  return VERDICT_BADGE[String(verdict).toLowerCase()] ?? 'gray'
}

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, options)
  if (!res.ok) {
    let message = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: string; message?: string }
      message = body.detail ?? body.message ?? message
    } catch {
      // keep status fallback
    }
    throw new Error(message)
  }
  return res.json() as Promise<T>
}

function basename(path: string) {
  return path.split(/[\\/]/).filter(Boolean).pop() ?? path
}

function formatTrackName(item: AuthenticityItem) {
  const artist = item.artist?.trim()
  const title = item.title?.trim()
  if (artist && title) return `${artist} - ${title}`
  if (title) return title
  if (artist) return artist
  return basename(item.file_path)
}

function formatCutoff(value: number | null) {
  if (value == null) return '—'
  return `${(value / 1000).toFixed(1)} kHz`
}

function formatSampleRate(value: number | null) {
  if (value == null) return '—'
  return `${(value / 1000).toFixed(value % 1000 === 0 ? 0 : 1)}k`
}

function formatPercent(value: number | null) {
  if (value == null) return '—'
  return `${Math.round(value * 100)}%`
}

function labelize(value: string) {
  return value.replace(/_/g, ' ')
}

function Spectrogram({ trackId }: { trackId: number }) {
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    setFailed(false)
  }, [trackId])

  if (failed) {
    return (
      <div className="flex min-h-44 items-center justify-center rounded-xl border border-[#2a2d3a] bg-[#0f1117] px-4 text-center text-sm text-slate-400">
        Spectrogram unavailable (file may have been moved to trash)
      </div>
    )
  }

  return (
    <img
      src={`/api/authenticity/${trackId}/spectrogram`}
      alt="Track spectrogram"
      className="max-h-[360px] w-full rounded-xl border border-[#2a2d3a] bg-[#0f1117] object-contain"
      onError={() => setFailed(true)}
    />
  )
}

export default function Authenticity() {
  const [summary, setSummary] = useState<AuthenticitySummary | null>(null)
  const [summaryLoading, setSummaryLoading] = useState(true)
  const [filter, setFilter] = useState<FilterValue>('transcode')
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [items, setItems] = useState<AuthenticityItem[]>([])
  const [total, setTotal] = useState(0)
  const [itemsLoading, setItemsLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [ingesting, setIngesting] = useState(false)
  const [selected, setSelected] = useState<AuthenticityItem | null>(null)

  const loadSummary = useCallback(async () => {
    setSummaryLoading(true)
    try {
      setSummary(await fetchJson<AuthenticitySummary>('/api/authenticity/summary'))
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Failed to load authenticity summary')
    } finally {
      setSummaryLoading(false)
    }
  }, [])

  const loadItems = useCallback(
    async (offset: number, mode: 'replace' | 'append', signal?: AbortSignal) => {
      if (mode === 'replace') setItemsLoading(true)
      else setLoadingMore(true)

      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
      })
      if (filter !== 'all') params.set('verdict', filter)
      if (debouncedQuery.trim()) params.set('q', debouncedQuery.trim())

      try {
        const data = await fetchJson<AuthenticityListResponse>(`/api/authenticity?${params.toString()}`, { signal })
        setTotal(data.total)
        setItems((current) => (mode === 'append' ? [...current, ...data.items] : data.items))
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        toast.error(error instanceof Error ? error.message : 'Failed to load authenticity results')
      } finally {
        if (mode === 'replace') setItemsLoading(false)
        else setLoadingMore(false)
      }
    },
    [debouncedQuery, filter],
  )

  useEffect(() => {
    void loadSummary()
  }, [loadSummary])

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query), 300)
    return () => window.clearTimeout(timer)
  }, [query])

  useEffect(() => {
    const controller = new AbortController()
    void loadItems(0, 'replace', controller.signal)
    return () => controller.abort()
  }, [loadItems])

  const statValues = useMemo(() => {
    const counts = summary?.counts ?? {}
    return {
      lossless: counts.lossless ?? 0,
      suspect: counts.suspect ?? 0,
      transcode: counts.transcode ?? 0,
      coverage: `${(summary?.coverage_pct ?? 0).toFixed(1)}%`,
      analyzed: `${summary?.analyzed ?? 0}/${summary?.total_flac ?? 0}`,
      recuedFixed: `${summary?.recue?.triggered ?? 0}/${summary?.recue?.fixed ?? 0}`,
    }
  }, [summary])

  const handleIngest = async () => {
    setIngesting(true)
    try {
      const data = await fetchJson<IngestResponse>('/api/authenticity/ingest', { method: 'POST' })
      await loadSummary()
      void loadItems(0, 'replace')
      toast.success(`Ingested ${data.ingested}; matched ${data.matched}; unmatched ${data.unmatched}`)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Failed to re-ingest scan')
    } finally {
      setIngesting(false)
    }
  }

  const hasMore = items.length < total

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-3">
            <div className="rounded-xl bg-[#d4a017]/15 p-2.5 text-[#f0c95c]">
              <ShieldCheck className="h-6 w-6" />
            </div>
            <h1 className="font-[family-name:var(--font-family-display)] text-3xl font-bold text-white">
              Authenticity
            </h1>
          </div>
          <p className="text-sm text-slate-400">
            Inspect FLAC files for lossless, suspicious, and transcoded audio signatures.
          </p>
        </div>
        <Button variant="secondary" onClick={handleIngest} loading={ingesting}>
          <RefreshCw className="h-4 w-4" />
          Re-ingest Scan
        </Button>
      </div>

      {summaryLoading ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
          {Array.from({ length: 5 }).map((_, index) => (
            <Skeleton key={index} className="h-32 rounded-xl" />
          ))}
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
          <StatCard icon={CheckCircle2} label="Lossless" value={statValues.lossless} accent="green" />
          <StatCard icon={AlertTriangle} label="Suspect" value={statValues.suspect} accent="amber" />
          <StatCard icon={XCircle} label="Transcode" value={statValues.transcode} accent="red" />
          <StatCard icon={RefreshCw} label="Recued / Fixed" value={statValues.recuedFixed} accent="green" />
          <StatCard
            icon={FileAudio}
            label="Coverage"
            value={statValues.coverage}
            subtitle={`${statValues.analyzed} FLAC analyzed`}
            accent="blue"
          />
        </div>
      )}

      <GlassCard className="overflow-hidden">
        <div className="space-y-4 border-b border-[#2a2d3a] p-4">
          <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
            <div className="flex flex-wrap gap-2">
              {FILTERS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => setFilter(option.value)}
                  className={`rounded-lg border px-3 py-2 text-sm font-medium transition-colors ${
                    filter === option.value
                      ? 'border-[#d4a017]/50 bg-[#d4a017]/15 text-[#f0c95c]'
                      : 'border-[#2a2d3a] bg-[#1a1d27] text-slate-400 hover:bg-[#2a2d3a] hover:text-slate-200'
                  }`}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <div className="relative w-full xl:w-80">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search artist, title, album, path"
                className="w-full rounded-lg border border-[#2a2d3a] bg-[#0f1117] py-2 pl-9 pr-3 text-sm text-slate-200 placeholder:text-slate-600 focus:border-[#d4a017]/50 focus:outline-none"
              />
            </div>
          </div>
          <div className="text-sm text-slate-500">
            Showing {items.length} of {total} result{total === 1 ? '' : 's'}
          </div>
        </div>

        {itemsLoading ? (
          <SkeletonTable rows={7} cols={5} />
        ) : items.length === 0 ? (
          <EmptyState
            icon={Waves}
            title="No authenticity results"
            description="No tracks match the selected verdict and search terms."
          />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-left text-sm">
                <thead className="border-b border-[#2a2d3a] text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-4 py-3 font-medium">Artist - Title</th>
                    <th className="px-4 py-3 font-medium">Verdict</th>
                    <th className="px-4 py-3 font-medium">Cutoff</th>
                    <th className="px-4 py-3 font-medium">Source Guess</th>
                    <th className="px-4 py-3 font-medium">Sample Rate</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#2a2d3a]">
                  {items.map((item) => (
                    <tr
                      key={item.track_id}
                      onClick={() => setSelected(item)}
                      className="cursor-pointer transition-colors hover:bg-[#2a2d3a]/45"
                    >
                      <td className="max-w-[420px] px-4 py-3">
                        <div className="truncate font-medium text-slate-200">{formatTrackName(item)}</div>
                        {item.album && <div className="truncate text-xs text-slate-500">{item.album}</div>}
                      </td>
                      <td className="px-4 py-3">
                        <Badge variant={verdictVariant(item.verdict)}>{labelize(item.verdict)}</Badge>
                      </td>
                      <td className="px-4 py-3 tabular-nums text-slate-300">{formatCutoff(item.cutoff_hz)}</td>
                      <td className="px-4 py-3 text-slate-300">{item.source_guess ?? '—'}</td>
                      <td className="px-4 py-3 tabular-nums text-slate-300">{formatSampleRate(item.sample_rate)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {hasMore && (
              <div className="flex justify-center border-t border-[#2a2d3a] p-4">
                <Button variant="secondary" onClick={() => loadItems(items.length, 'append')} loading={loadingMore}>
                  Load more
                </Button>
              </div>
            )}
          </>
        )}
      </GlassCard>

      <Modal
        open={selected != null}
        onClose={() => setSelected(null)}
        title={selected ? formatTrackName(selected) : 'Authenticity details'}
      >
        {selected && (
          <div className="space-y-4">
            <Spectrogram trackId={selected.track_id} />
            <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs uppercase tracking-wide text-slate-500">Verdict</dt>
                <dd className="mt-1">
                  <Badge variant={verdictVariant(selected.verdict)}>{labelize(selected.verdict)}</Badge>
                </dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-slate-500">Confidence</dt>
                <dd className="mt-1 text-slate-200">{formatPercent(selected.confidence)}</dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-slate-500">Cutoff</dt>
                <dd className="mt-1 text-slate-200">{formatCutoff(selected.cutoff_hz)}</dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-slate-500">Source Guess</dt>
                <dd className="mt-1 text-slate-200">{selected.source_guess ?? '—'}</dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-slate-500">Sample Rate</dt>
                <dd className="mt-1 text-slate-200">{formatSampleRate(selected.sample_rate)}</dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-xs uppercase tracking-wide text-slate-500">File Path</dt>
                <dd className="mt-1 break-all text-slate-300">{selected.file_path}</dd>
              </div>
            </dl>
          </div>
        )}
      </Modal>
    </div>
  )
}
