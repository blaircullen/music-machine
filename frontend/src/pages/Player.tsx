import { useParams, useNavigate } from 'react-router-dom'
import { ChevronLeft, SkipBack, Play, Pause, SkipForward, ThumbsUp, ThumbsDown, RefreshCw } from 'lucide-react'
import { usePlayer } from '../hooks/usePlayer'

function formatTime(secs: number): string {
  if (!isFinite(secs) || isNaN(secs)) return '0:00'
  const m = Math.floor(secs / 60)
  const s = Math.floor(secs % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

export default function Player() {
  const { stationId } = useParams<{ stationId: string }>()
  const navigate = useNavigate()
  const id = Number(stationId)
  const { state, controls, currentTrack } = usePlayer(id)

  const progress = state.duration > 0 ? (state.currentTime / state.duration) * 100 : 0
  const feedbackState = currentTrack ? state.feedbackSent[currentTrack.track_id] : undefined

  return (
    <div className="fixed inset-0 bg-[#13151f] flex flex-col overflow-hidden">
      {/* Top bar */}
      <div className="flex items-center justify-between px-5 pt-14 pb-2">
        <button
          onClick={() => navigate(-1)}
          className="p-2 -ml-2 text-white/40 hover:text-white transition-colors"
        >
          <ChevronLeft className="w-5 h-5" />
        </button>
        <span className="text-[11px] text-white/40 uppercase tracking-[0.2em] font-medium">
          Now Playing
        </span>
        <button
          onClick={controls.regenerate}
          disabled={state.loading}
          title="Regenerate playlist"
          className="p-2 -mr-2 text-white/40 hover:text-[#d4a017] transition-colors disabled:opacity-30"
        >
          <RefreshCw className={`w-4 h-4 ${state.loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {/* Artwork */}
      <div className="px-5 pt-4 pb-6">
        {state.loading ? (
          <div className="w-full aspect-square rounded-2xl bg-white/5 animate-pulse flex items-center justify-center">
            <div className="w-8 h-8 border-2 border-[#d4a017] border-t-transparent rounded-full animate-spin" />
          </div>
        ) : state.error ? (
          <div className="w-full aspect-square rounded-2xl bg-white/5 flex items-center justify-center px-8 text-center">
            <p className="text-white/40 text-sm">{state.error}</p>
          </div>
        ) : currentTrack ? (
          <div className="relative w-full aspect-square">
            <img
              src={currentTrack.artwork_url}
              alt={currentTrack.album}
              className="w-full h-full rounded-2xl object-cover shadow-2xl"
              onError={e => { e.currentTarget.style.display = 'none' }}
            />
            <div className="w-full h-full rounded-2xl bg-[#1e2130] absolute inset-0 -z-10 flex items-center justify-center">
              <div className="text-6xl opacity-10">♫</div>
            </div>
            {state.buffering && (
              <div className="absolute inset-0 flex items-center justify-center rounded-2xl bg-black/40">
                <div className="w-8 h-8 border-2 border-white border-t-transparent rounded-full animate-spin" />
              </div>
            )}
          </div>
        ) : (
          <div className="w-full aspect-square rounded-2xl bg-[#1e2130] flex items-center justify-center">
            <div className="text-6xl opacity-10">♫</div>
          </div>
        )}
      </div>

      {/* Track info */}
      <div className="px-6 pb-5">
        {currentTrack ? (
          <>
            <p className="text-white font-black text-3xl leading-tight truncate font-[family-name:var(--font-family-display)]">
              {currentTrack.artist}
            </p>
            <p className="text-white/50 text-base mt-1 truncate font-medium">
              {currentTrack.title}
            </p>
          </>
        ) : (
          <>
            <div className="h-9 w-48 bg-white/5 rounded-lg animate-pulse" />
            <div className="h-5 w-32 bg-white/5 rounded-lg animate-pulse mt-2" />
          </>
        )}
      </div>

      {/* Scrub row: ThumbsDown | bar | ThumbsUp */}
      <div className="px-4 pb-6">
        <div className="flex items-center gap-3">
          {/* Thumbs down */}
          <button
            onClick={controls.thumbDown}
            disabled={!currentTrack || !!feedbackState || state.loading}
            title="Skip and avoid"
            className={`flex-none transition-all disabled:opacity-30 ${
              feedbackState === 'down'
                ? 'text-red-400 scale-110'
                : 'text-white/30 hover:text-red-400'
            }`}
          >
            <ThumbsDown className="w-5 h-5" />
          </button>

          {/* Scrub bar + timestamps */}
          <div className="flex-1 min-w-0">
            <input
              type="range"
              min={0}
              max={state.duration || 100}
              value={state.currentTime}
              onChange={e => controls.seek(Number(e.target.value))}
              disabled={!currentTrack || state.loading}
              className="w-full h-1 appearance-none bg-white/15 rounded-full cursor-pointer disabled:cursor-default"
              style={{
                background: `linear-gradient(to right, #d4a017 ${progress}%, rgba(255,255,255,0.15) ${progress}%)`,
              }}
            />
            <div className="flex justify-between text-[10px] text-white/25 mt-1.5">
              <span>{formatTime(state.currentTime)}</span>
              <span>{formatTime(state.duration)}</span>
            </div>
          </div>

          {/* Thumbs up */}
          <button
            onClick={controls.thumbUp}
            disabled={!currentTrack || !!feedbackState || state.loading}
            title="Love it"
            className={`flex-none transition-all disabled:opacity-30 ${
              feedbackState === 'up'
                ? 'text-[#d4a017] scale-110'
                : 'text-white/30 hover:text-[#d4a017]'
            }`}
          >
            <ThumbsUp className="w-5 h-5" />
          </button>
        </div>
      </div>

      {/* Playback controls */}
      <div className="flex items-center justify-center gap-10 pb-10">
        <button
          onClick={controls.prev}
          disabled={state.loading}
          className="p-3 text-white/50 hover:text-white transition-colors disabled:opacity-30"
        >
          <SkipBack className="w-7 h-7" />
        </button>

        <button
          onClick={controls.togglePlay}
          disabled={state.loading || !!state.error}
          className="w-18 h-18 rounded-full border-2 border-white/60 flex items-center justify-center hover:border-white active:scale-95 transition-all disabled:opacity-30"
          style={{ width: '72px', height: '72px' }}
        >
          {state.playing
            ? <Pause className="w-8 h-8 text-white" />
            : <Play className="w-8 h-8 text-white ml-1" />
          }
        </button>

        <button
          onClick={controls.next}
          disabled={state.loading}
          className="p-3 text-white/50 hover:text-white transition-colors disabled:opacity-30"
        >
          <SkipForward className="w-7 h-7" />
        </button>
      </div>

      {/* Queue position */}
      {state.tracks.length > 0 && (
        <div className="text-center pb-6 text-[10px] text-white/15">
          {state.currentIndex + 1} / {state.tracks.length}
        </div>
      )}
    </div>
  )
}
