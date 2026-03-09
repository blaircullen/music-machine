import { lazy, Suspense, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Menu } from 'lucide-react'
import { Sidebar } from './components/layout/Sidebar'

const Library = lazy(() => import('./pages/Library'))
const JobLog = lazy(() => import('./pages/JobLog'))
const Upgrades = lazy(() => import('./pages/Upgrades'))
const Tagger = lazy(() => import('./pages/Tagger'))
const Stations = lazy(() => import('./pages/Stations'))
const Settings = lazy(() => import('./pages/Settings'))
const Duplicates = lazy(() => import('./pages/Duplicates'))
const Trash = lazy(() => import('./pages/Trash'))
const Player = lazy(() => import('./pages/Player'))

function PageFallback() {
  return (
    <div className="flex items-center justify-center h-64">
      <div className="w-5 h-5 border-2 border-[#d4a017] border-t-transparent rounded-full animate-spin" />
    </div>
  )
}

export default function App() {
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  return (
    <BrowserRouter>
      <Routes>
        {/* Full-screen player — no sidebar, no shell chrome */}
        <Route
          path="/listen/:stationId"
          element={
            <Suspense fallback={<PageFallback />}>
              <Player />
            </Suspense>
          }
        />

        {/* All other routes share the sidebar shell */}
        <Route
          path="/*"
          element={
            <div className="min-h-screen flex">
              <Sidebar mobileOpen={mobileNavOpen} onMobileClose={() => setMobileNavOpen(false)} />

              {/* Mobile header */}
              <div className="fixed top-0 left-0 right-0 h-14 bg-[#13151f] border-b border-[#2a2d3a] flex items-center px-4 z-30 lg:hidden">
                <button
                  onClick={() => setMobileNavOpen(true)}
                  aria-label="Open navigation"
                  className="p-2 text-slate-400 hover:text-white rounded-lg transition-colors"
                >
                  <Menu className="w-5 h-5" />
                </button>
                <span className="ml-3 text-sm font-bold text-white font-[family-name:var(--font-family-display)]">
                  Music Machine
                </span>
              </div>

              <main className="lg:ml-[220px] flex-1 p-4 pt-18 lg:p-8 lg:pt-8 min-h-screen min-w-0 overflow-x-hidden">
                <Suspense fallback={<PageFallback />}>
                  <Routes>
                    <Route path="/" element={<Navigate to="/stations" replace />} />
                    <Route path="/library" element={<Library />} />
                    <Route path="/jobs" element={<JobLog />} />
                    <Route path="/upgrades" element={<Upgrades />} />
                    <Route path="/tagger" element={<Tagger />} />
                    <Route path="/stations" element={<Stations />} />
                    <Route path="/settings" element={<Settings />} />
                    <Route path="/duplicates" element={<Duplicates />} />
                    <Route path="/trash" element={<Trash />} />
                  </Routes>
                </Suspense>
              </main>
            </div>
          }
        />
      </Routes>
    </BrowserRouter>
  )
}
