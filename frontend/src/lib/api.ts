// Typed API client — all calls go through request()

export interface StatsResponse {
  total_tracks: number
  flac_count: number
  lossy_count: number
  dupes_found: number
  upgrades_pending: number
  lossy_upgrades_pending: number
  hires_upgrades_pending: number
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

export interface Station {
  id: number
  name: string
  seed_track_ids: number[]
  plex_playlist_name: string
  track_count: number
  last_refreshed: string | null
  created_at: string
}

export interface StationCreate {
  name: string
  seed_track_ids: number[]
  plex_playlist_name?: string
}

export interface StationRefreshStatus {
  running: boolean
  error: string | null
}

export interface SeedTrack {
  id: number
  artist: string
  title: string
  album: string
  duration: number | null
}

export interface QueueTrack {
  track_id: number
  artist: string
  album_artist: string | null
  album: string
  title: string
  duration: number | null
  format: string
  stream_url: string
  artwork_url: string
}

export interface StationQueue {
  station_id: number
  tracks: QueueTrack[]
  generated_at: string | null
}

export interface AnalysisStats {
  total_tracks: number
  analyzed_count: number
  queued_count: number
  coverage_pct: number
}

// Fingerprint Engine
export interface FingerprintStats {
  total_tracks: number
  processed: number
  unprocessed: number
  status_counts: Record<string, number>
  matched: number
  flagged: number
  unmatched: number
  failed: number
  audd: {
    month_requests: number
    month_cost_dollars: number
    today_requests: number
    budget_dollars: number
    budget_remaining_dollars: number
    within_budget: boolean
  }
  genre_distribution: Array<{ matched_genre: string; count: number }>
  source_counts: Record<string, number>
}

export interface FingerprintProgress {
  running: boolean
  phase: string
  processed: number
  total: number
  matched: number
  auto_approved: number
  flagged: number
  unmatched: number
  failed: number
  elapsed_s: number
  current_file: string | null
  dry_run: boolean
}

export interface FingerprintReviewItem {
  id: number
  track_id: number
  file_path: string
  format: string
  bitrate: number
  duration: number
  current_artist: string | null
  current_title: string | null
  current_album: string | null
  current_album_artist: string | null
  current_track_number: number | null
  matched_artist: string | null
  matched_title: string | null
  matched_album: string | null
  matched_album_artist: string | null
  matched_year: number | null
  matched_track_number: number | null
  matched_disc_number: number | null
  matched_genre: string | null
  matched_isrc: string | null
  matched_label: string | null
  matched_composer: string | null
  matched_cover_art_url: string | null
  composite_confidence: number
  match_source: string | null
  acoustid_score: number | null
  status: string
}

export interface FingerprintHistoryItem {
  id: number
  track_id: number
  file_path: string
  original_artist: string | null
  original_title: string | null
  original_album: string | null
  matched_artist: string | null
  matched_title: string | null
  matched_album: string | null
  matched_genre: string | null
  composite_confidence: number
  match_source: string | null
  fp_status: string
  snapshot_at: string
}

export interface PaginatedResponse<T> {
  items: T[]
  total: number
  limit: number
  offset: number
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

// Stations
export function getStations(): Promise<Station[]> {
  return request<Station[]>('/stations')
}

export function getStation(id: number): Promise<Station> {
  return request<Station>(`/stations/${id}`)
}

export function createStation(data: StationCreate): Promise<Station> {
  return request<Station>('/stations', { method: 'POST', body: JSON.stringify(data) })
}

export function updateStation(id: number, data: Partial<StationCreate>): Promise<Station> {
  return request<Station>(`/stations/${id}`, { method: 'PUT', body: JSON.stringify(data) })
}

export function deleteStation(id: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/stations/${id}`, { method: 'DELETE' })
}

export function refreshStation(id: number): Promise<{ ok: boolean; error?: string }> {
  return request<{ ok: boolean; error?: string }>(`/stations/${id}/refresh`, { method: 'POST' })
}

export function getStationRefreshStatus(id: number): Promise<StationRefreshStatus> {
  return request<StationRefreshStatus>(`/stations/${id}/status`)
}

export function searchStationTracks(q: string, signal?: AbortSignal): Promise<SeedTrack[]> {
  return request<SeedTrack[]>(`/stations/search/tracks?q=${encodeURIComponent(q)}&limit=20`, { signal })
}

// Sonic
export function getAnalysisStats(): Promise<AnalysisStats> {
  return request<AnalysisStats>('/sonic/stats')
}

export function getStationQueue(stationId: number): Promise<StationQueue> {
  return request<StationQueue>(`/sonic/queue/${stationId}`)
}

export function postStationFeedback(
  stationId: number,
  trackId: number,
  signal: 'up' | 'down',
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/sonic/feedback/${stationId}`, {
    method: 'POST',
    body: JSON.stringify({ track_id: trackId, signal }),
  })
}

// Fingerprint Engine
export function getFingerprintStats(): Promise<FingerprintStats> {
  return request<FingerprintStats>('/fingerprint/stats')
}

export function getFingerprintProgress(): Promise<FingerprintProgress> {
  return request<FingerprintProgress>('/fingerprint/progress')
}

export function getFingerprintReview(opts?: {
  status?: string
  min_confidence?: number
  max_confidence?: number
  source?: string
  limit?: number
  offset?: number
}): Promise<PaginatedResponse<FingerprintReviewItem>> {
  const params = new URLSearchParams()
  if (opts?.status) params.set('status', opts.status)
  if (opts?.min_confidence != null) params.set('min_confidence', String(opts.min_confidence))
  if (opts?.max_confidence != null) params.set('max_confidence', String(opts.max_confidence))
  if (opts?.source) params.set('source', opts.source)
  if (opts?.limit) params.set('limit', String(opts.limit))
  if (opts?.offset) params.set('offset', String(opts.offset))
  const qs = params.toString()
  return request<PaginatedResponse<FingerprintReviewItem>>(`/fingerprint/review${qs ? `?${qs}` : ''}`)
}

export function approveFingerprintResult(id: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/fingerprint/review/${id}/approve`, { method: 'POST' })
}

export function batchApproveFingerprintResults(opts: { ids?: number[]; min_confidence?: number }): Promise<{ ok: boolean; approved: number }> {
  return request<{ ok: boolean; approved: number }>('/fingerprint/review/batch-approve', {
    method: 'POST',
    body: JSON.stringify(opts),
  })
}

export function editFingerprintResult(id: number, metadata: Record<string, unknown>): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/fingerprint/review/${id}/edit`, {
    method: 'POST',
    body: JSON.stringify(metadata),
  })
}

export function skipFingerprintResult(id: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/fingerprint/review/${id}/skip`, { method: 'POST' })
}

export function getFingerprintUnmatched(limit?: number, offset?: number): Promise<PaginatedResponse<FingerprintReviewItem>> {
  const params = new URLSearchParams()
  if (limit) params.set('limit', String(limit))
  if (offset) params.set('offset', String(offset))
  const qs = params.toString()
  return request<PaginatedResponse<FingerprintReviewItem>>(`/fingerprint/unmatched${qs ? `?${qs}` : ''}`)
}

export function getFingerprintHistory(limit?: number, offset?: number): Promise<PaginatedResponse<FingerprintHistoryItem>> {
  const params = new URLSearchParams()
  if (limit) params.set('limit', String(limit))
  if (offset) params.set('offset', String(offset))
  const qs = params.toString()
  return request<PaginatedResponse<FingerprintHistoryItem>>(`/fingerprint/history${qs ? `?${qs}` : ''}`)
}

export function rollbackFingerprint(id: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/fingerprint/rollback/${id}`, { method: 'POST' })
}

export function postFingerprintRun(dry_run: boolean = false): Promise<{ ok: boolean; dry_run: boolean }> {
  return request<{ ok: boolean; dry_run: boolean }>(`/fingerprint/run?dry_run=${dry_run}`, { method: 'POST' })
}

export function stopFingerprintEngine(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>('/fingerprint/stop', { method: 'POST' })
}

export function getMbStatus(): Promise<{ available: boolean; type: string }> {
  return request<{ available: boolean; type: string }>('/fingerprint/mb-status')
}
