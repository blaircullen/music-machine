import { useEffect, useState } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { Trash2, RotateCcw, HardDrive } from 'lucide-react'
import { GlassCard, StatCard, Button, EmptyState, Modal, SkeletonTable, toast } from '../components/ui'

interface TrashItem {
  id: number
  track_id: number
  artist: string
  title: string
  album: string
  format: string
  source_path: string
  dest_path: string
  performed_at: string
}

interface TrashSize {
  size_bytes: number
  size_mb: number
}

export default function Trash() {
  const [items, setItems] = useState<TrashItem[]>([])
  const [size, setSize] = useState<TrashSize | null>(null)
  const [loading, setLoading] = useState(true)
  const [restoring, setRestoring] = useState<number | null>(null)
  const [showConfirm, setShowConfirm] = useState(false)

  const fetchData = async () => {
    setLoading(true)
    try {
      const [itemsRes, sizeRes] = await Promise.all([
        fetch('/api/trash/').then(r => r.json()),
        fetch('/api/trash/size').then(r => r.json()),
      ])
      setItems(itemsRes)
      setSize(sizeRes)
    } catch {
      toast.error('Failed to load trash')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchData() }, [])

  const handleRestore = async (item: TrashItem) => {
    setRestoring(item.id)
    try {
      await fetch(`/api/trash/${item.id}/restore`, { method: 'POST' })
      setItems(prev => prev.filter(i => i.id !== item.id))
      toast.success(`Restored: ${item.artist} - ${item.title}`)
    } catch {
      toast.error('Failed to restore')
    } finally {
      setRestoring(null)
    }
  }

  const handleEmpty = async () => {
    setShowConfirm(false)
    try {
      const res = await fetch('/api/trash/empty', { method: 'POST' })
      if (!res.ok) {
        const err = await res.json().catch(() => null)
        toast.error(err?.detail || `Failed to empty trash (${res.status})`)
        return
      }
      const data = await res.json()
      toast.success(`Trash emptied — ${data.deleted} file${data.deleted !== 1 ? 's' : ''} deleted`)
      fetchData()
    } catch {
      toast.error('Failed to empty trash — network error')
    }
  }

  if (loading) {
    return (
      <div className="space-y-6">
        <div className="h-8 w-32 skeleton-shimmer rounded-lg" />
        <div className="h-24 w-48 skeleton-shimmer rounded-2xl" />
        <SkeletonTable rows={5} cols={6} />
      </div>
    )
  }

  const sizeDisplay = size
    ? size.size_mb >= 1024
      ? `${(size.size_mb / 1024).toFixed(2)} GB`
      : `${size.size_mb} MB`
    : '0 MB'

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold font-[family-name:var(--font-family-display)]">Trash</h2>
        <Button variant="danger" onClick={() => setShowConfirm(true)} disabled={items.length === 0}>
          <Trash2 className="w-4 h-4" />
          Empty Trash
        </Button>
      </div>

      <Modal
        open={showConfirm}
        onClose={() => setShowConfirm(false)}
        title="Confirm Empty Trash"
        message={`This will permanently delete ${items.length} item${items.length !== 1 ? 's' : ''}. This action cannot be undone.`}
        confirmLabel="Delete Permanently"
        confirmVariant="danger"
        onConfirm={handleEmpty}
      />

      <div className="w-fit">
        <StatCard icon={HardDrive} label="Trash Size" value={sizeDisplay} />
      </div>

      {items.length === 0 ? (
        <EmptyState
          icon={Trash2}
          title="Trash is empty"
          description="Resolved duplicates and skipped files will appear here."
        />
      ) : (
        <GlassCard className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-glass-border text-base-400 text-left">
                  <th scope="col" className="px-4 py-3 font-medium">Artist</th>
                  <th scope="col" className="px-4 py-3 font-medium">Title</th>
                  <th scope="col" className="px-4 py-3 font-medium">Album</th>
                  <th scope="col" className="px-4 py-3 font-medium">Format</th>
                  <th scope="col" className="px-4 py-3 font-medium">Original Path</th>
                  <th scope="col" className="px-4 py-3 font-medium">Trashed</th>
                  <th scope="col" className="px-4 py-3 font-medium"></th>
                </tr>
              </thead>
              <AnimatePresence>
                <tbody>
                  {items.map(item => (
                    <motion.tr
                      key={item.id}
                      layout
                      exit={{ opacity: 0, x: 50 }}
                      className="border-b border-glass-border/50 hover:bg-base-800/30 transition-colors"
                    >
                      <td className="px-4 py-3">{item.artist || '--'}</td>
                      <td className="px-4 py-3">{item.title || '--'}</td>
                      <td className="px-4 py-3 text-base-400">{item.album || '--'}</td>
                      <td className="px-4 py-3 uppercase font-mono text-xs">{item.format || '--'}</td>
                      <td className="px-4 py-3 text-base-500 font-mono text-xs max-w-xs truncate" title={item.source_path}>
                        {item.source_path}
                      </td>
                      <td className="px-4 py-3 text-base-400 text-xs">
                        {new Date(item.performed_at).toLocaleDateString()}
                      </td>
                      <td className="px-4 py-3">
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => handleRestore(item)}
                          disabled={restoring === item.id}
                        >
                          <RotateCcw className="w-3.5 h-3.5" />
                          Restore
                        </Button>
                      </td>
                    </motion.tr>
                  ))}
                </tbody>
              </AnimatePresence>
            </table>
          </div>
        </GlassCard>
      )}
    </div>
  )
}
