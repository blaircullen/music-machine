import { useState, useEffect } from 'react'
import { NavLink } from 'react-router-dom'
import { LayoutDashboard, Library, ScrollText, ArrowUpCircle, Wand2 } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { getStats } from '../../lib/api'

interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  end?: boolean
}

const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/library', label: 'Library', icon: Library },
  { to: '/upgrades', label: 'Upgrades', icon: ArrowUpCircle },
  { to: '/tagger', label: 'MetaTagger', icon: Wand2 },
  { to: '/jobs', label: 'Job Log', icon: ScrollText },
]

function useMusicGrabberStatus() {
  const [connected, setConnected] = useState<boolean | null>(null)

  useEffect(() => {
    let mounted = true

    const check = () => {
      getStats()
        .then(() => { if (mounted) setConnected(true) })
        .catch(() => { if (mounted) setConnected(false) })
    }

    check()
    const timer = setInterval(check, 15_000)
    return () => { mounted = false; clearInterval(timer) }
  }, [])

  return connected
}

export function Sidebar() {
  const mgConnected = useMusicGrabberStatus()

  return (
    <aside className="fixed left-0 top-0 h-screen w-[220px] flex flex-col bg-[#13151f] border-r border-[#2a2d3a] z-40">
      {/* Logo */}
      <div className="flex items-center gap-3 px-4 h-16 border-b border-[#2a2d3a] shrink-0">
        <img
          src="/logo.png"
          alt="Music Machine"
          className="w-9 h-9 rounded-lg shadow-[0_0_12px_rgba(212,160,23,0.3)]"
        />
        <div className="flex flex-col leading-tight">
          <span className="text-sm font-bold text-white tracking-tight" style={{ fontFamily: 'Syne, sans-serif' }}>
            Music Machine
          </span>
          <span className="text-[10px] text-[#d4a017]/70 tracking-widest uppercase font-medium">
            M<sup>2</sup>
          </span>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 py-4 px-3 space-y-0.5 overflow-y-auto">
        {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              [
                'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-150 relative',
                isActive
                  ? 'text-[#f0c95c] bg-[#d4a017]/10 border-l-2 border-[#d4a017]'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-[#1a1d27] border-l-2 border-transparent',
              ].join(' ')
            }
          >
            <Icon className="w-4 h-4 shrink-0" />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>

      {/* MusicGrabber status + copyright */}
      <div className="px-5 py-4 border-t border-[#2a2d3a] shrink-0 space-y-3">
        <div className="flex items-center gap-2">
          <span
            className={`w-2 h-2 rounded-full shrink-0 ${
              mgConnected === null
                ? 'bg-slate-600'
                : mgConnected
                ? 'bg-[#22c55e] shadow-[0_0_6px_rgba(34,197,94,0.6)]'
                : 'bg-[#ef4444] shadow-[0_0_6px_rgba(239,68,68,0.6)]'
            }`}
          />
          <span className="text-xs text-slate-500">
            {mgConnected === null
              ? 'Checking...'
              : mgConnected
              ? 'Connected'
              : 'Disconnected'}
          </span>
        </div>
        <p className="text-[10px] text-slate-600 text-center">&copy; 2026 Shawnee Digital</p>
      </div>
    </aside>
  )
}
