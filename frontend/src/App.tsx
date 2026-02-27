import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { Sidebar } from './components/layout/Sidebar'
import Dashboard from './pages/Dashboard'
import Library from './pages/Library'
import JobLog from './pages/JobLog'

export default function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen flex">
        <Sidebar />
        <main className="ml-[220px] flex-1 p-8 min-h-screen">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/library" element={<Library />} />
            <Route path="/jobs" element={<JobLog />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
