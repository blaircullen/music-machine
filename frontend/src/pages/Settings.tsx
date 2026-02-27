import { useEffect, useState } from 'react'
import { GlassCard, Button, Skeleton, toast } from '../components/ui'

interface SettingsData {
  music_path: string
  trash_path: string
  fingerprint_threshold: string
  squid_rate_limit: string
  auto_resolve_threshold: string
  upgrade_scan_folders: string
}

async function apiPut<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json() as Promise<T>
}

export default function Settings() {
  const [settings, setSettings] = useState<SettingsData | null>(null)
  const [loading, setLoading] = useState(true)
  const [threshold, setThreshold] = useState('0.85')
  const [rateLimit, setRateLimit] = useState('3')
  const [autoResolve, setAutoResolve] = useState('0')
  const [upgradeFolders, setUpgradeFolders] = useState('')

  useEffect(() => {
    fetch('/api/settings/')
      .then((r) => r.json())
      .then((data: SettingsData) => {
        setSettings(data)
        setThreshold(data.fingerprint_threshold)
        setRateLimit(data.squid_rate_limit)
        setAutoResolve(data.auto_resolve_threshold || '0')
        setUpgradeFolders(data.upgrade_scan_folders || '')
        setLoading(false)
      })
      .catch(() => {
        toast.error('Failed to load settings')
        setLoading(false)
      })
  }, [])

  const handleSave = async () => {
    try {
      const data = await apiPut<SettingsData>('/api/settings/', {
        fingerprint_threshold: threshold,
        squid_rate_limit: rateLimit,
        auto_resolve_threshold: autoResolve,
        upgrade_scan_folders: upgradeFolders,
      })
      setSettings(data)
      toast.success('Settings saved')
    } catch {
      toast.error('Failed to save settings')
    }
  }

  if (loading) {
    return (
      <div className="space-y-6 max-w-2xl">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-64 rounded-2xl" />
      </div>
    )
  }

  return (
    <div className="space-y-6 max-w-2xl">
      <h2 className="text-2xl font-bold">Settings</h2>

      <GlassCard className="p-6 space-y-6">
        <div>
          <label className="block text-sm font-medium text-base-400 mb-1.5">Music Path</label>
          <div className="px-4 py-2.5 bg-base-800/50 border border-glass-border rounded-xl text-sm text-base-500 font-mono">
            {settings?.music_path}
          </div>
        </div>

        <div>
          <label className="block text-sm font-medium text-base-400 mb-1.5">Trash Path</label>
          <div className="px-4 py-2.5 bg-base-800/50 border border-glass-border rounded-xl text-sm text-base-500 font-mono">
            {settings?.trash_path}
          </div>
        </div>

        <div>
          <label htmlFor="threshold" className="block text-sm font-medium text-base-400 mb-1.5">
            Fingerprint Threshold: {threshold}
          </label>
          <input
            id="threshold"
            type="range"
            min="0.5"
            max="1.0"
            step="0.01"
            value={threshold}
            onChange={(e) => setThreshold(e.target.value)}
            className="w-full h-2 bg-base-700 rounded-full appearance-none cursor-pointer"
          />
        </div>

        <div>
          <label htmlFor="rate-limit" className="block text-sm font-medium text-base-400 mb-1.5">
            Rate Limit (seconds)
          </label>
          <input
            id="rate-limit"
            type="number"
            min="1"
            step="1"
            value={rateLimit}
            onChange={(e) => setRateLimit(e.target.value)}
            className="w-full px-4 py-2.5 bg-base-800/50 border border-glass-border rounded-xl text-sm"
          />
        </div>

        <div>
          <label htmlFor="upgrade-folders" className="block text-sm font-medium text-base-400 mb-1.5">
            Upgrade Scan Folders
          </label>
          <input
            id="upgrade-folders"
            type="text"
            value={upgradeFolders}
            onChange={(e) => setUpgradeFolders(e.target.value)}
            placeholder="/music/mp3s/, /music/iTunes/"
            className="w-full px-4 py-2.5 bg-base-800/50 border border-glass-border rounded-xl text-sm"
          />
        </div>

        <div>
          <label htmlFor="auto-resolve" className="block text-sm font-medium text-base-400 mb-1.5">
            Auto-Resolve Threshold: {Math.round(parseFloat(autoResolve) * 100)}%
          </label>
          <input
            id="auto-resolve"
            type="range"
            min="0"
            max="1.0"
            step="0.05"
            value={autoResolve}
            onChange={(e) => setAutoResolve(e.target.value)}
            className="w-full h-2 bg-base-700 rounded-full appearance-none cursor-pointer"
          />
        </div>

        <div className="pt-2">
          <Button onClick={handleSave}>Save Settings</Button>
        </div>
      </GlassCard>
    </div>
  )
}
