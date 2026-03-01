import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { Sidebar } from './components/layout/Sidebar'

const Dashboard = lazy(() => import('./pages/Dashboard'))
const Library = lazy(() => import('./pages/Library'))
const JobLog = lazy(() => import('./pages/JobLog'))
const Upgrades = lazy(() => import('./pages/Upgrades'))
const Tagger = lazy(() => import('./pages/Tagger'))

function PageFallback() {
  return (
    <div className="flex items-center justify-center h-64">
      <div className="w-5 h-5 border-2 border-[#d4a017] border-t-transparent rounded-full animate-spin" />
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen flex">
        <Sidebar />
        <main className="ml-[220px] flex-1 p-8 min-h-screen min-w-0 overflow-x-hidden">
          <Suspense fallback={<PageFallback />}>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/library" element={<Library />} />
              <Route path="/jobs" element={<JobLog />} />
              <Route path="/upgrades" element={<Upgrades />} />
              <Route path="/tagger" element={<Tagger />} />
            </Routes>
          </Suspense>
        </main>
      </div>
    </BrowserRouter>
  )
}
