// Typed API client — all calls go through request()

export interface StatsResponse {
  total_tracks: number
  flac_count: number
  lossy_count: number
  dupes_found: number
  upgrades_pending: number
  upgrades_completed: number
  library_size_gb: number
  formats: Array<{ format: string; count: number }>
}

export interface ScanStatus {
  running: boolean
  phase: string
  progress: number
  total: number
  current_file: string
  elapsed_s: number
}

export interface Track {
  id: number
  path: string
  format: string
  bitrate: number
  bit_depth: number | null
  sample_rate: number
  artist: string
  album: string
  title: string
  duration: number
  quality_score: number
  is_winner: boolean
}

export interface DupeGroup {
  id: number
  confidence: number
  match_type: string
  resolved: boolean
  tracks: Track[]
}

export interface Upgrade {
  id: number
  track_id: number
  artist: string
  album: string
  title: string
  format: string
  bitrate: number
  status: 'pending' | 'found' | 'approved' | 'downloading' | 'completed' | 'failed' | 'skipped'
  match_quality: string
  staging_path: string | null
  created_at: string
  error_msg?: string | null
}

export interface UpgradeStatus {
  running: boolean
  phase: string
  searched: number
  found: number
  downloading: number
  completed: number
  failed: number
  current_track: string | null
  current_artist: string | null
  current_title: string | null
  current_album: string | null
  current_step: 'downloading' | 'importing' | null
  current_bytes: number
  current_total_bytes: number
  download_index: number
  download_total: number
}

export interface Job {
  id: number
  job_type: string
  status: 'running' | 'completed' | 'failed' | 'pending'
  created_at: string
  updated_at: string
  error_msg: string | null
  details: Record<string, unknown> | null
}

export interface TrashItem {
  id: number
  original_path: string
  trash_path: string
  moved_at: string
  file_size: number
}

export interface TrashStats {
  count: number
  size_bytes: number
}

export interface TaggerStatus {
  running: boolean
  phase: 'idle' | 'scanning' | 'tagging' | 'complete' | 'failed'
  processed: number
  total: number
  tagged: number
  failed: number
  skipped: number
  current_file: string | null
  elapsed_s: number
}

export interface TaggerResult {
  id: number
  track_id: number | null
  file_path: string
  status: 'pending' | 'matched' | 'tagged' | 'failed' | 'skipped'
  acoustid_score: number | null
  mb_recording_id: string | null
  mb_release_id: string | null
  matched_artist: string | null
  matched_title: string | null
  matched_album: string | null
  cover_art_url: string | null
  error_msg: string | null
  created_at: string
  updated_at: string
}

const BASE = '/api'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  })
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: string; message?: string }
      msg = body.detail ?? body.message ?? msg
    } catch {
      // ignore parse failures
    }
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

// Stats
export function getStats(): Promise<StatsResponse> {
  return request<StatsResponse>('/stats')
}

// Scan
export function postScan(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>('/scan', { method: 'POST' })
}

export function getScanStatus(): Promise<ScanStatus> {
  return request<ScanStatus>('/scan/status')
}

// Duplicates
export function getDupes(): Promise<DupeGroup[]> {
  return request<DupeGroup[]>('/dupes')
}

export function resolveDupe(id: number): Promise<{ moved: number }> {
  return request<{ moved: number }>(`/dupes/${id}/resolve`, { method: 'POST' })
}

export function resolveAllDupes(): Promise<{ resolved: number }> {
  return request<{ resolved: number }>('/dupes/resolve-all', { method: 'POST' })
}

// Upgrades
export function getUpgrades(): Promise<Upgrade[]> {
  return request<Upgrade[]>('/upgrades')
}

export function postUpgradesSearch(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>('/upgrades/search', { method: 'POST' })
}

export function getUpgradesStatus(): Promise<UpgradeStatus> {
  return request<UpgradeStatus>('/upgrades/status')
}

export function approveUpgrade(id: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/upgrades/${id}/approve`, { method: 'POST' })
}

export function approveAllUpgrades(): Promise<{ approved: number }> {
  return request<{ approved: number }>('/upgrades/approve-all', { method: 'POST' })
}

export function postUpgradesDownload(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>('/upgrades/download', { method: 'POST' })
}

export function skipUpgrade(id: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/upgrades/${id}/skip`, { method: 'POST' })
}

// Jobs
export function getJobs(): Promise<Job[]> {
  return request<Job[]>('/jobs')
}

export function retryJob(id: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/jobs/${id}/retry`, { method: 'POST' })
}

// Trash
export function getTrash(): Promise<TrashItem[]> {
  return request<TrashItem[]>('/trash')
}

export function restoreTrashItem(id: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/trash/${id}/restore`, { method: 'POST' })
}

export function emptyTrash(): Promise<{ deleted: number }> {
  return request<{ deleted: number }>('/trash/empty', { method: 'POST' })
}

export function getTrashStats(): Promise<TrashStats> {
  return request<TrashStats>('/trash/stats')
}

// Tagger
export function postTaggerRun(opts?: { path?: string; force?: boolean; dry_run?: boolean }): Promise<{ ok: boolean; error?: string }> {
  const params = new URLSearchParams()
  if (opts?.path) params.set('path', opts.path)
  if (opts?.force) params.set('force', 'true')
  if (opts?.dry_run) params.set('dry_run', 'true')
  const qs = params.toString()
  return request<{ ok: boolean; error?: string }>(`/tagger/run${qs ? `?${qs}` : ''}`, { method: 'POST' })
}

export function getTaggerStatus(): Promise<TaggerStatus> {
  return request<TaggerStatus>('/tagger/status')
}

export function getTaggerResults(status?: string): Promise<TaggerResult[]> {
  const qs = status ? `?status=${status}` : ''
  return request<TaggerResult[]>(`/tagger/results${qs}`)
}

export function retryTagJob(id: number): Promise<{ ok: boolean; status?: string; error?: string }> {
  return request<{ ok: boolean; status?: string; error?: string }>(`/tagger/${id}/retry`, { method: 'POST' })
}

export function skipTagJob(id: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/tagger/${id}/skip`, { method: 'POST' })
}
