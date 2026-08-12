import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { Button } from './Button'

const FOCUSABLE = 'button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])'

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
  const panelRef = useRef<HTMLDivElement>(null)
  const previousFocusRef = useRef<HTMLElement | null>(null)

  // Restore focus on close, capture trigger on open
  useEffect(() => {
    if (open) {
      previousFocusRef.current = document.activeElement as HTMLElement
      // Move focus to first focusable element in the modal
      requestAnimationFrame(() => {
        const first = panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE)[0]
        first?.focus()
      })
    } else {
      previousFocusRef.current?.focus()
      previousFocusRef.current = null
    }
  }, [open])

  // Close on Escape + focus trap
  useEffect(() => {
    if (!open) return
    function handler(e: KeyboardEvent) {
      if (e.key === 'Escape') { onClose(); return }
      if (e.key !== 'Tab') return
      const panel = panelRef.current
      if (!panel) return
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (e.shiftKey) {
        if (document.activeElement === first) { e.preventDefault(); last.focus() }
      } else {
        if (document.activeElement === last) { e.preventDefault(); first.focus() }
      }
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
      <div ref={panelRef} className="relative w-full max-w-md rounded-xl bg-[#1a1d27] border border-[#2a2d3a] shadow-2xl p-6">
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
