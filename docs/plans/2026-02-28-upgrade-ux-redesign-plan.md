# Upgrade UX Redesign Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the fire-and-forget "scan everything" upgrade flow with a targeted, batchable scan launcher, scan coverage visibility, and a keyboard-driven card review mode.

**Architecture:** Backend gains four new/modified endpoints (scoped search, coverage summary, unscanned list, approve-hi-res). Frontend gains a scan launcher modal, a coverage bar, an unscanned tab, and a review mode card view toggled from the found tab.

**Tech Stack:** FastAPI + SQLite (backend), React 18 + Vite + Tailwind (frontend). Tests: pytest + FastAPI TestClient (backend), manual smoke test (frontend).

---

## Task 1: Backend — Accept scan scope params

**Files:**
- Modify: `backend/routes/upgrades.py`

The existing `POST /api/upgrades/search` takes no body. We add an optional JSON body with four fields, thread them into the worker, and use them to filter candidates.

**Step 1: Write the failing test**

Create `backend/tests/test_upgrades_scoped.py`:

```python
from fastapi.testclient import TestClient
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from main import app

client = TestClient(app)

def test_search_accepts_scope_body():
    """POST /api/upgrades/search should accept scope params without error."""
    res = client.post("/api/upgrades/search", json={
        "format_filter": "mp3",
        "unscanned_only": True,
        "batch_size": 10,
        "artist_filter": None,
    })
    # Returns ok:true or ok:false (already running) — either is valid
    assert res.status_code == 200
    data = res.json()
    assert "ok" in data
```

**Step 2: Run test to verify it fails**

```bash
cd /Users/sunygxc/projects/plex-dedup
PYTHONPATH=backend pytest backend/tests/test_upgrades_scoped.py -v
```

Expected: FAIL — `422 Unprocessable Entity` (FastAPI rejects unknown body) or function signature error.

**Step 3: Add Pydantic model + wire into endpoint**

In `backend/routes/upgrades.py`, after the imports add:

```python
from pydantic import BaseModel

class ScanScope(BaseModel):
    format_filter: str = "all_lossy"   # "all_lossy" | "mp3" | "aac" | "m4a" | "ogg" | "wma" | "opus" | "cd_flac"
    unscanned_only: bool = True
    batch_size: int = 50               # max album groups per run (0 = no limit)
    artist_filter: str | None = None   # partial match on artist name
```

Replace the existing `start_upgrade_search` route:

```python
@router.post("/search")
def start_upgrade_search(scope: ScanScope | None = None):
    """Start a scoped background slskd search for FLAC upgrades."""
    if upgrade_search_status["running"]:
        return {"ok": False, "error": "Upgrade search already in progress"}

    s = scope or ScanScope()
    t = threading.Thread(
        target=_run_upgrade_search_worker,
        kwargs={
            "format_filter": s.format_filter,
            "unscanned_only": s.unscanned_only,
            "batch_size": s.batch_size,
            "artist_filter": s.artist_filter,
        },
        daemon=True,
    )
    t.start()
    return {"ok": True}
```

**Step 4: Update `_run_upgrade_search_worker` signature**

Change the function signature:

```python
def _run_upgrade_search_worker(
    format_filter: str = "all_lossy",
    unscanned_only: bool = True,
    batch_size: int = 50,
    artist_filter: str | None = None,
):
```

Replace the candidate query block (lines 110–128) with a scoped version:

```python
        # Build format filter
        if format_filter == "cd_flac":
            fmt_clause = "t.format = 'flac' AND (t.bit_depth IS NULL OR t.bit_depth <= 16)"
            params = []
        elif format_filter == "all_lossy":
            lossy_placeholders = ",".join("?" * len(LOSSY_FORMATS))
            fmt_clause = f"t.format IN ({lossy_placeholders})"
            params = list(LOSSY_FORMATS)
        else:
            # Specific lossy format
            fmt_clause = "t.format = ?"
            params = [format_filter]

        unscanned_clause = ""
        if unscanned_only:
            unscanned_clause = """
                AND t.id NOT IN (
                    SELECT track_id FROM upgrade_queue
                    WHERE status NOT IN ('failed', 'skipped')
                )
            """

        artist_clause = ""
        if artist_filter:
            artist_clause = "AND LOWER(t.artist) LIKE ?"
            params.append(f"%{artist_filter.lower()}%")

        query = f"""
            SELECT t.* FROM tracks t
            WHERE ({fmt_clause})
            AND t.status = 'active'
            {unscanned_clause}
            {artist_clause}
            ORDER BY t.artist, t.album, t.track_number
        """
        if scan_limit > 0:
            query += f" LIMIT {scan_limit}"

        with get_db() as db:
            candidates = db.execute(query, params).fetchall()
```

After building `grouped` and `individual` dicts, add batch limiting:

```python
        # Apply batch_size limit (count by album groups + individual tracks)
        if batch_size > 0:
            group_keys = list(grouped.keys())[:batch_size]
            grouped = {k: grouped[k] for k in group_keys}
            # Fill remaining batch slots with individual tracks
            remaining = max(0, batch_size - len(grouped))
            individual = individual[:remaining]
```

**Step 5: Run test to verify it passes**

```bash
PYTHONPATH=backend pytest backend/tests/test_upgrades_scoped.py -v
```

Expected: PASS

**Step 6: Commit**

```bash
cd /Users/sunygxc/projects/plex-dedup
git add backend/routes/upgrades.py backend/tests/test_upgrades_scoped.py
git commit -m "feat: scoped upgrade scan (format filter, batch size, artist filter, unscanned_only)"
```

---

## Task 2: Backend — Coverage summary endpoint

**Files:**
- Modify: `backend/routes/upgrades.py`

**Step 1: Write the failing test**

Append to `backend/tests/test_upgrades_scoped.py`:

```python
def test_coverage_endpoint():
    res = client.get("/api/upgrades/coverage")
    assert res.status_code == 200
    data = res.json()
    for key in ("total_candidates", "scanned", "unscanned", "found", "completed"):
        assert key in data
        assert isinstance(data[key], int)
```

**Step 2: Run test to verify it fails**

```bash
PYTHONPATH=backend pytest backend/tests/test_upgrades_scoped.py::test_coverage_endpoint -v
```

Expected: FAIL — 404 Not Found

**Step 3: Add the endpoint**

Append to `backend/routes/upgrades.py`:

```python
@router.get("/coverage")
def get_coverage():
    """Return scan coverage counts across the library."""
    with get_db() as db:
        lossy_placeholders = ",".join("?" * len(LOSSY_FORMATS))
        # Total upgrade candidates (lossy + CD-FLAC)
        total = db.execute(
            f"""SELECT COUNT(*) FROM tracks
                WHERE (format IN ({lossy_placeholders})
                       OR (format = 'flac' AND (bit_depth IS NULL OR bit_depth <= 16)))
                AND status = 'active'""",
            list(LOSSY_FORMATS),
        ).fetchone()[0]

        # Scanned = has an entry in upgrade_queue (any status except failed/skipped counts)
        scanned = db.execute(
            """SELECT COUNT(DISTINCT track_id) FROM upgrade_queue
               WHERE status NOT IN ('failed', 'skipped')"""
        ).fetchone()[0]

        found = db.execute(
            "SELECT COUNT(*) FROM upgrade_queue WHERE status = 'found'"
        ).fetchone()[0]

        completed = db.execute(
            "SELECT COUNT(*) FROM upgrade_queue WHERE status = 'completed'"
        ).fetchone()[0]

    return {
        "total_candidates": total,
        "scanned": scanned,
        "unscanned": max(0, total - scanned),
        "found": found,
        "completed": completed,
    }
```

**Step 4: Run test to verify it passes**

```bash
PYTHONPATH=backend pytest backend/tests/test_upgrades_scoped.py::test_coverage_endpoint -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add backend/routes/upgrades.py backend/tests/test_upgrades_scoped.py
git commit -m "feat: GET /api/upgrades/coverage endpoint"
```

---

## Task 3: Backend — Unscanned tracks list endpoint

**Files:**
- Modify: `backend/routes/upgrades.py`

**Step 1: Write the failing test**

Append to `backend/tests/test_upgrades_scoped.py`:

```python
def test_unscanned_endpoint():
    res = client.get("/api/upgrades/unscanned")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    if data:
        row = data[0]
        for key in ("track_id", "artist", "album", "title", "format", "bitrate"):
            assert key in row
```

**Step 2: Run test to verify it fails**

```bash
PYTHONPATH=backend pytest backend/tests/test_upgrades_scoped.py::test_unscanned_endpoint -v
```

Expected: FAIL — 404

**Step 3: Add the endpoint**

Append to `backend/routes/upgrades.py`:

```python
@router.get("/unscanned")
def list_unscanned(limit: int = 500):
    """Return active lossy/CD-FLAC tracks that have never been searched."""
    with get_db() as db:
        lossy_placeholders = ",".join("?" * len(LOSSY_FORMATS))
        rows = db.execute(
            f"""SELECT t.id AS track_id, t.artist, t.album, t.title,
                       t.format, t.bitrate, t.bit_depth, t.sample_rate
                FROM tracks t
                LEFT JOIN upgrade_queue uq ON uq.track_id = t.id
                    AND uq.status NOT IN ('failed', 'skipped')
                WHERE (
                    t.format IN ({lossy_placeholders})
                    OR (t.format = 'flac' AND (t.bit_depth IS NULL OR t.bit_depth <= 16))
                )
                AND t.status = 'active'
                AND uq.track_id IS NULL
                ORDER BY t.artist, t.album, t.track_number
                LIMIT ?""",
            list(LOSSY_FORMATS) + [limit],
        ).fetchall()
    return [dict(r) for r in rows]
```

**Step 4: Run test to verify it passes**

```bash
PYTHONPATH=backend pytest backend/tests/test_upgrades_scoped.py::test_unscanned_endpoint -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add backend/routes/upgrades.py backend/tests/test_upgrades_scoped.py
git commit -m "feat: GET /api/upgrades/unscanned endpoint"
```

---

## Task 4: Backend — Approve hi-res only endpoint

**Files:**
- Modify: `backend/routes/upgrades.py`

**Step 1: Write the failing test**

Append to `backend/tests/test_upgrades_scoped.py`:

```python
def test_approve_hi_res_endpoint():
    res = client.post("/api/upgrades/approve-hi-res")
    assert res.status_code == 200
    data = res.json()
    assert "ok" in data
    assert "approved" in data
    assert isinstance(data["approved"], int)
```

**Step 2: Run test to verify it fails**

```bash
PYTHONPATH=backend pytest backend/tests/test_upgrades_scoped.py::test_approve_hi_res_endpoint -v
```

Expected: FAIL — 404

**Step 3: Add the endpoint**

Append to `backend/routes/upgrades.py` (place it before or after `approve_all_upgrades`):

```python
@router.post("/approve-hi-res")
def approve_hi_res_upgrades():
    """Approve only hi-res quality found items (match_quality = 'hi_res')."""
    with get_db() as db:
        result = db.execute(
            """UPDATE upgrade_queue
               SET status = 'approved', updated_at = CURRENT_TIMESTAMP
               WHERE status = 'found'
                 AND match_quality = 'hi_res'
                 AND slskd_username IS NOT NULL
                 AND slskd_filename IS NOT NULL"""
        )
    return {"ok": True, "approved": result.rowcount}
```

**Step 4: Run all backend tests**

```bash
PYTHONPATH=backend pytest backend/tests/ -v
```

Expected: All PASS

**Step 5: Commit**

```bash
git add backend/routes/upgrades.py backend/tests/test_upgrades_scoped.py
git commit -m "feat: POST /api/upgrades/approve-hi-res endpoint"
```

---

## Task 5: Frontend — Scan Launcher Modal

**Files:**
- Modify: `frontend/src/pages/Upgrades.tsx`

Replace the inline "Find Upgrades" button with a modal that exposes the four scope params. No new file needed — add modal state and JSX inline.

**Step 1: Add modal state at the top of the `Upgrades` component**

After the existing state declarations (around line 53), add:

```typescript
const [scanModalOpen, setScanModalOpen] = useState(false)
const [scanScope, setScanScope] = useState({
  format_filter: 'all_lossy',
  unscanned_only: true,
  batch_size: 50,
  artist_filter: '',
})
```

**Step 2: Update `handleScan` to accept scope**

Replace the existing `handleScan` (lines 146–155):

```typescript
const handleScan = async () => {
  setScanModalOpen(false)
  setSearchRequested(true)
  try {
    await fetch('/api/upgrades/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...scanScope,
        artist_filter: scanScope.artist_filter || null,
        batch_size: Number(scanScope.batch_size),
      }),
    })
    toast.success('Search started')
  } catch {
    setSearchRequested(false)
    toast.error('Failed to start upgrade scan')
  }
}
```

**Step 3: Replace the "Find Upgrades" button with a modal trigger**

Replace the existing scan button (the `<button onClick={handleScan} ...>` block) with:

```tsx
<button
  onClick={() => setScanModalOpen(true)}
  disabled={isSearching || isDownloading}
  className="px-4 py-2 rounded-xl text-sm font-medium transition-all duration-300 inline-flex items-center gap-2 bg-base-700/80 text-base-300 hover:bg-base-600 border border-glass-border backdrop-blur-md disabled:opacity-40 disabled:cursor-not-allowed shadow-sm hover:shadow-md"
>
  {isSearching ? <Loader2 className="w-4 h-4 animate-spin text-lime" /> : <Search className="w-4 h-4 text-lime" />}
  {isSearching ? 'Searching...' : 'Find Upgrades'}
</button>
```

**Step 4: Add modal JSX**

Add the modal just before the closing `</div>` of the component return (before the final `</div>`):

```tsx
{/* Scan Launcher Modal */}
{scanModalOpen && (
  <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setScanModalOpen(false)}>
    <div className="bg-base-800 border border-glass-border rounded-2xl p-6 w-full max-w-md shadow-2xl" onClick={e => e.stopPropagation()}>
      <h3 className="text-lg font-semibold mb-4">Find Upgrades</h3>

      <div className="space-y-4">
        {/* Format filter */}
        <div>
          <label className="text-xs text-base-400 uppercase tracking-wider mb-2 block">Format</label>
          <div className="flex flex-wrap gap-2">
            {[
              { value: 'all_lossy', label: 'All Lossy' },
              { value: 'mp3', label: 'MP3' },
              { value: 'aac', label: 'AAC' },
              { value: 'cd_flac', label: 'CD FLAC → Hi-Res' },
            ].map(opt => (
              <button
                key={opt.value}
                onClick={() => setScanScope(s => ({ ...s, format_filter: opt.value }))}
                className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-all ${
                  scanScope.format_filter === opt.value
                    ? 'bg-lime/20 border-lime/40 text-lime'
                    : 'bg-base-700 border-base-600 text-base-400 hover:text-base-200'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>

        {/* Unscanned only toggle */}
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium">Unscanned only</p>
            <p className="text-xs text-base-500">Skip tracks already searched</p>
          </div>
          <button
            onClick={() => setScanScope(s => ({ ...s, unscanned_only: !s.unscanned_only }))}
            className={`w-11 h-6 rounded-full transition-all relative ${scanScope.unscanned_only ? 'bg-lime' : 'bg-base-600'}`}
          >
            <span className={`absolute top-0.5 w-5 h-5 rounded-full bg-white shadow transition-all ${scanScope.unscanned_only ? 'left-5' : 'left-0.5'}`} />
          </button>
        </div>

        {/* Batch size */}
        <div>
          <label className="text-xs text-base-400 uppercase tracking-wider mb-1 block">
            Batch size (albums per run)
          </label>
          <input
            type="number"
            min={1}
            max={500}
            value={scanScope.batch_size}
            onChange={e => setScanScope(s => ({ ...s, batch_size: parseInt(e.target.value) || 50 }))}
            className="w-full bg-base-700 border border-base-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-lime/50"
          />
        </div>

        {/* Artist filter */}
        <div>
          <label className="text-xs text-base-400 uppercase tracking-wider mb-1 block">
            Artist filter (optional)
          </label>
          <input
            type="text"
            placeholder="e.g. Pink Floyd"
            value={scanScope.artist_filter}
            onChange={e => setScanScope(s => ({ ...s, artist_filter: e.target.value }))}
            className="w-full bg-base-700 border border-base-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-lime/50 placeholder:text-base-600"
          />
        </div>
      </div>

      <div className="flex gap-3 mt-6">
        <button
          onClick={() => setScanModalOpen(false)}
          className="flex-1 px-4 py-2 rounded-xl text-sm font-medium bg-base-700 text-base-400 hover:bg-base-600 border border-base-600 transition-all"
        >
          Cancel
        </button>
        <button
          onClick={handleScan}
          className="flex-1 px-4 py-2 rounded-xl text-sm font-semibold bg-lime text-white hover:bg-lime/90 transition-all inline-flex items-center justify-center gap-2"
        >
          <Search className="w-4 h-4" />
          Start Scan
        </button>
      </div>
    </div>
  </div>
)}
```

**Step 5: Build and smoke test**

```bash
cd /Users/sunygxc/projects/plex-dedup/frontend && npm run build
```

Expected: Build succeeds with no TypeScript errors. Open the app, click "Find Upgrades" — modal appears with all four controls. Selecting a format highlights it. Toggle works. "Start Scan" fires the search and closes modal.

**Step 6: Commit**

```bash
cd /Users/sunygxc/projects/plex-dedup
git add frontend/src/pages/Upgrades.tsx
git commit -m "feat: scan launcher modal with format filter, batch size, artist filter, unscanned toggle"
```

---

## Task 6: Frontend — Coverage bar + Unscanned tab

**Files:**
- Modify: `frontend/src/pages/Upgrades.tsx`

**Step 1: Add coverage fetch**

After the existing `fetchQueue` callback, add:

```typescript
const [coverage, setCoverage] = useState<{
  total_candidates: number
  scanned: number
  unscanned: number
  found: number
  completed: number
} | null>(null)

const fetchCoverage = useCallback(async () => {
  try {
    const res = await fetch('/api/upgrades/coverage')
    setCoverage(await res.json())
  } catch {}
}, [])
```

Add `fetchCoverage` to the initial useEffect and to the phase-transition refresh:

```typescript
// In the existing useEffect([fetchQueue]):
useEffect(() => {
  fetchQueue()
  fetchCoverage()
}, [fetchQueue, fetchCoverage])

// And in the justFinished branch, also call fetchCoverage()
```

**Step 2: Add unscanned list state + fetch**

```typescript
const [unscannedTracks, setUnscannedTracks] = useState<Array<{
  track_id: number
  artist: string
  album: string
  title: string
  format: string
  bitrate: number
}>>([])

const fetchUnscanned = useCallback(async () => {
  try {
    const res = await fetch('/api/upgrades/unscanned')
    setUnscannedTracks(await res.json())
  } catch {}
}, [])
```

**Step 3: Update `FilterTab` type and tabs array**

Change the type:
```typescript
type FilterTab = 'all' | 'found' | 'approved' | 'completed' | 'skipped' | 'unscanned'
```

Update tabs array:
```typescript
const tabs: { key: FilterTab; label: string; count: number }[] = [
  { key: 'all', label: 'All', count: queue.length },
  { key: 'found', label: 'Found', count: foundCount },
  { key: 'approved', label: 'Approved', count: approvedCount },
  { key: 'completed', label: 'Completed', count: queue.filter(i => i.status === 'completed').length },
  { key: 'skipped', label: 'Skipped', count: queue.filter(i => i.status === 'skipped').length },
  { key: 'unscanned', label: 'Never Scanned', count: coverage?.unscanned ?? 0 },
]
```

When `filterTab` changes to `'unscanned'`, fetch unscanned list:
```typescript
useEffect(() => {
  if (filterTab === 'unscanned') {
    fetchUnscanned()
  } else {
    fetchQueue()
    fetchCoverage()
  }
}, [filterTab, fetchQueue, fetchCoverage, fetchUnscanned])
```

**Step 4: Add coverage bar JSX**

Add just below the page heading `<h2>` and before the button row:

```tsx
{coverage && (
  <div className="text-xs text-base-500 flex gap-4 flex-wrap">
    <span>
      Coverage: <span className="text-base-300 font-medium">{coverage.scanned.toLocaleString()} scanned</span>
      {' · '}
      <button
        className="text-amber-400 font-medium hover:underline"
        onClick={() => setFilterTab('unscanned')}
      >
        {coverage.unscanned.toLocaleString()} never scanned
      </button>
      {' · '}
      <span className="text-lime font-medium">{coverage.found.toLocaleString()} found</span>
      {' · '}
      <span className="text-base-400">{coverage.completed.toLocaleString()} completed</span>
    </span>
  </div>
)}
```

**Step 5: Add unscanned tab content**

In the conditional render area (where the table is shown), add a branch for `filterTab === 'unscanned'`:

```tsx
) : filterTab === 'unscanned' ? (
  unscannedTracks.length === 0 ? (
    <EmptyState
      icon={CheckCircle}
      title="All candidates scanned"
      description="Every lossy and CD-FLAC track has been searched at least once."
    />
  ) : (
    <GlassCard className="overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-glass-border text-base-400 text-left">
              <th className="px-4 py-3 font-medium">Artist</th>
              <th className="px-4 py-3 font-medium">Album</th>
              <th className="px-4 py-3 font-medium">Title</th>
              <th className="px-4 py-3 font-medium">Format</th>
              <th className="px-4 py-3 font-medium text-right">Action</th>
            </tr>
          </thead>
          <tbody>
            {unscannedTracks.map(track => (
              <tr key={track.track_id} className="border-b border-glass-border/30 hover:bg-white/[0.02]">
                <td className="px-4 py-3 text-base-300 font-medium">{track.artist || '--'}</td>
                <td className="px-4 py-3 text-base-400">{track.album || '--'}</td>
                <td className="px-4 py-3 text-base-400">{track.title || '--'}</td>
                <td className="px-4 py-3">
                  <span className="bg-base-700/80 px-1.5 py-0.5 rounded-md border border-base-600/50 font-mono text-xs uppercase">
                    {track.format} {track.bitrate > 0 ? `${track.bitrate}k` : ''}
                  </span>
                </td>
                <td className="px-4 py-3 text-right">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => {
                      setScanScope(s => ({
                        ...s,
                        artist_filter: track.artist || '',
                        unscanned_only: false,
                        batch_size: 5,
                      }))
                      handleScanAlbum(track.artist, track.album)
                    }}
                  >
                    <Search className="w-3 h-3" />
                    Scan album
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </GlassCard>
  )
```

**Step 6: Add `handleScanAlbum`**

```typescript
const handleScanAlbum = async (artist: string, album: string) => {
  setSearchRequested(true)
  try {
    await fetch('/api/upgrades/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        format_filter: 'all_lossy',
        unscanned_only: false,
        batch_size: 5,
        artist_filter: artist || null,
      }),
    })
    toast.success(`Scanning: ${artist} — ${album}`)
  } catch {
    setSearchRequested(false)
    toast.error('Failed to start scan')
  }
}
```

**Step 7: Build and smoke test**

```bash
cd /Users/sunygxc/projects/plex-dedup/frontend && npm run build
```

Expected: No TypeScript errors. Verify: coverage bar appears with accurate counts; "Never Scanned" tab shows unscanned tracks; clicking "Scan album" kicks off a targeted search.

**Step 8: Commit**

```bash
cd /Users/sunygxc/projects/plex-dedup
git add frontend/src/pages/Upgrades.tsx
git commit -m "feat: coverage bar, unscanned tab, per-album scan trigger"
```

---

## Task 7: Frontend — Review Mode (card view + keyboard shortcuts)

**Files:**
- Modify: `frontend/src/pages/Upgrades.tsx`

This is the most substantial frontend change. We add a `reviewMode` boolean and a `reviewIndex` cursor that let the user step through found items one at a time.

**Step 1: Add review mode state**

```typescript
const [reviewMode, setReviewMode] = useState(false)
const [reviewIndex, setReviewIndex] = useState(0)
```

**Step 2: Add approve-hi-res handler**

```typescript
const handleApproveHiRes = async () => {
  try {
    const res = await fetch('/api/upgrades/approve-hi-res', { method: 'POST' })
    const data = await res.json()
    setQueue(prev => prev.map(item =>
      item.status === 'found' && item.match_quality === 'hi_res'
        ? { ...item, status: 'approved' }
        : item
    ))
    toast.success(`Approved ${data.approved} hi-res matches`)
  } catch {
    toast.error('Failed to approve hi-res')
  }
}
```

**Step 3: Compute review items**

```typescript
const foundItems = useMemo(
  () => sortedQueue.filter(i => i.status === 'found' || i.status === 'pending'),
  [sortedQueue]
)
const reviewItem = foundItems[reviewIndex] ?? null
```

**Step 4: Add keyboard handler**

```typescript
useEffect(() => {
  if (!reviewMode) return
  const handler = (e: KeyboardEvent) => {
    if (e.key === 'a' || e.key === 'A' || e.key === ' ') {
      e.preventDefault()
      if (reviewItem) handleApprove(reviewItem.id, reviewItem.artist, reviewItem.title)
        .then(() => setReviewIndex(i => Math.min(i + 1, foundItems.length - 1)))
    }
    if (e.key === 's' || e.key === 'S' || e.key === 'x' || e.key === 'X') {
      e.preventDefault()
      if (reviewItem) handleSkip(reviewItem.id, reviewItem.artist, reviewItem.title)
        .then(() => setReviewIndex(i => Math.min(i + 1, foundItems.length - 1)))
    }
    if (e.key === 'ArrowRight') setReviewIndex(i => Math.min(i + 1, foundItems.length - 1))
    if (e.key === 'ArrowLeft') setReviewIndex(i => Math.max(i - 1, 0))
    if (e.key === 'Escape') setReviewMode(false)
  }
  window.addEventListener('keydown', handler)
  return () => window.removeEventListener('keydown', handler)
}, [reviewMode, reviewItem, foundItems.length, handleApprove, handleSkip])
```

Note: `handleApprove` and `handleSkip` currently don't return Promises. Update their signatures to `async` and ensure they return (they already do — they're `async` functions, so they implicitly return `Promise<void>`).

**Step 5: Add Review Mode toggle button**

In the tab row area, when `filterTab === 'found'` and `foundItems.length > 0`, show a toggle:

```tsx
{filterTab === 'found' && foundItems.length > 0 && (
  <div className="flex items-center gap-3 ml-auto">
    <button
      onClick={handleApproveHiRes}
      className="px-3 py-1.5 rounded-lg text-sm font-medium bg-green-900/40 text-green-400 border border-green-800/50 hover:bg-green-900/60 transition-all"
    >
      Approve all hi-res
    </button>
    <button
      onClick={() => { setReviewMode(m => !m); setReviewIndex(0) }}
      className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-all ${
        reviewMode
          ? 'bg-lime/20 border-lime/40 text-lime'
          : 'bg-base-700 border-base-600 text-base-400 hover:text-base-200'
      }`}
    >
      {reviewMode ? 'Exit Review' : 'Review Mode'}
    </button>
  </div>
)}
```

Place this in a flex container alongside the tab row (wrap the tab row and this in a `flex items-center gap-2` div).

**Step 6: Add card view JSX**

In the conditional render area, when `filterTab === 'found' && reviewMode`, show the card instead of the table:

```tsx
) : filterTab === 'found' && reviewMode ? (
  foundItems.length === 0 ? (
    <EmptyState icon={CheckCircle} title="All reviewed" description="No more found items to review." />
  ) : (
    <div className="flex flex-col items-center gap-6">
      {/* Progress */}
      <p className="text-sm text-base-500">
        {reviewIndex + 1} of {foundItems.length} found
        <span className="ml-3 text-base-600">· A/Space = approve · S/X = skip · ← → navigate · Esc = exit</span>
      </p>

      {reviewItem && (
        <GlassCard className="w-full max-w-2xl p-8">
          <div className="mb-1 text-base-400 text-sm font-medium">{reviewItem.artist}</div>
          <div className="text-xl font-bold text-base-100 mb-1">{reviewItem.album}</div>
          <div className="text-base-300 text-lg mb-6">{reviewItem.title}</div>

          <div className="flex gap-8 mb-8">
            <div>
              <p className="text-xs text-base-500 uppercase tracking-wider mb-1">Current</p>
              <p className="font-mono text-sm bg-base-700/80 px-2 py-1 rounded-lg border border-base-600/50 uppercase">
                {reviewItem.format} {reviewItem.bitrate > 0 ? `${reviewItem.bitrate}k` : ''}
              </p>
            </div>
            <div className="text-base-500 self-end mb-1">→</div>
            <div>
              <p className="text-xs text-base-500 uppercase tracking-wider mb-1">Match</p>
              <Badge variant={matchVariant(reviewItem.match_quality)}>
                {reviewItem.match_quality ?? 'unknown'}
              </Badge>
            </div>
          </div>

          <div className="flex gap-4">
            <Button
              variant="ghost"
              onClick={() => {
                handleSkip(reviewItem.id, reviewItem.artist, reviewItem.title)
                setReviewIndex(i => Math.min(i + 1, foundItems.length - 1))
              }}
              disabled={actionInProgress.has(reviewItem.id)}
              className="flex-1"
            >
              <XCircle className="w-4 h-4" />
              Skip (S)
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                handleApprove(reviewItem.id, reviewItem.artist, reviewItem.title)
                setReviewIndex(i => Math.min(i + 1, foundItems.length - 1))
              }}
              disabled={actionInProgress.has(reviewItem.id)}
              className="flex-1"
            >
              <CheckCircle className="w-4 h-4" />
              Approve (A)
            </Button>
          </div>
        </GlassCard>
      )}

      {/* Navigation arrows */}
      <div className="flex gap-4">
        <Button variant="secondary" onClick={() => setReviewIndex(i => Math.max(i - 1, 0))} disabled={reviewIndex === 0}>← Prev</Button>
        <Button variant="secondary" onClick={() => setReviewIndex(i => Math.min(i + 1, foundItems.length - 1))} disabled={reviewIndex >= foundItems.length - 1}>Next →</Button>
      </div>
    </div>
  )
```

**Step 7: Build and smoke test**

```bash
cd /Users/sunygxc/projects/plex-dedup/frontend && npm run build
```

Expected: No TypeScript errors. Verify: on the Found tab, "Review Mode" toggle appears. Entering review mode shows one card. Keyboard A/S advance through items. "Approve all hi-res" bulk-approves only hi_res items.

**Step 8: Commit**

```bash
cd /Users/sunygxc/projects/plex-dedup
git add frontend/src/pages/Upgrades.tsx
git commit -m "feat: review mode card view with keyboard shortcuts and approve-all-hi-res"
```

---

## Task 8: Deploy

**Step 1: Deploy to VM 101**

```bash
cd /Users/sunygxc/projects/plex-dedup
ssh-add --apple-load-keychain 2>/dev/null
rsync -av frontend/src/ sunygxc@10.0.0.75:/home/sunygxc/projects/plex-dedup/frontend/src/
ssh vm101 "cd /home/sunygxc/projects/plex-dedup && docker compose build --no-cache && docker compose up -d"
```

**Step 2: Smoke test live app**

- Open http://10.0.0.75:8686
- Upgrades page loads — coverage bar shows counts
- "Find Upgrades" opens modal with format/batch/artist controls
- "Never Scanned" tab shows unscanned tracks with "Scan album" buttons
- Found tab shows "Review Mode" toggle and "Approve all hi-res"
- Review mode card renders, keyboard A/S/←/→/Esc all work

**Step 3: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: post-deploy adjustments"
```
