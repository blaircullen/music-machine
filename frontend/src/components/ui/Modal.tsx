import { useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { Button } from './Button'

interface ModalProps {
  open: boolean
  onClose: () => void
  title: string
  message?: string
  confirmLabel?: string
  confirmVariant?: 'primary' | 'danger'
  onConfirm?: () => void | Promise<void>
  children?: ReactNode
}

export function Modal({
  open,
  onClose,
  title,
  message,
  confirmLabel = 'Confirm',
  confirmVariant = 'danger',
  onConfirm,
  children,
}: ModalProps) {
  // Close on Escape
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open, onClose])

  if (!open) return null

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        role="presentation"
        onClick={onClose}
      />
      {/* Panel */}
      <div className="relative w-full max-w-md rounded-xl bg-[#1a1d27] border border-[#2a2d3a] shadow-2xl p-6">
        <div className="flex items-start justify-between mb-4">
          <h3 id="modal-title" className="text-base font-semibold text-white">{title}</h3>
          <button
            onClick={onClose}
            aria-label="Close dialog"
            className="rounded-md p-1 text-slate-500 hover:text-slate-300 hover:bg-[#2a2d3a] transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {message && <p className="text-sm text-slate-400 mb-6">{message}</p>}
        {children && <div className="mb-6">{children}</div>}

        {onConfirm && (
          <div className="flex gap-3 justify-end">
            <Button variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button variant={confirmVariant} onClick={onConfirm}>
              {confirmLabel}
            </Button>
          </div>
        )}
      </div>
    </div>,
    document.body
  )
}
