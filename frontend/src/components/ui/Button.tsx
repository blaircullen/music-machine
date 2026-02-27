import { useState, useCallback, useEffect, type ReactNode } from 'react'
import { Loader2 } from 'lucide-react'

type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost'
type ButtonSize = 'sm' | 'md' | 'lg'

interface ButtonProps {
  children: ReactNode
  variant?: ButtonVariant
  size?: ButtonSize
  onClick?: () => void | Promise<void>
  disabled?: boolean
  loading?: boolean
  type?: 'button' | 'submit' | 'reset'
  className?: string
}

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    'bg-[#6c63ff] text-white hover:bg-[#7c73ff] active:bg-[#5c53ef] shadow-[0_0_16px_rgba(108,99,255,0.35)] hover:shadow-[0_0_24px_rgba(108,99,255,0.5)] font-semibold',
  secondary:
    'bg-[#1a1d27] text-slate-300 border border-[#2a2d3a] hover:bg-[#2a2d3a] hover:text-white',
  danger:
    'bg-[#ef4444]/10 text-[#f87171] border border-[#ef4444]/30 hover:bg-[#ef4444]/20 hover:text-red-300',
  ghost:
    'text-slate-400 hover:text-slate-200 hover:bg-[#2a2d3a]',
}

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: 'px-3 py-1.5 text-xs gap-1.5',
  md: 'px-4 py-2 text-sm gap-2',
  lg: 'px-5 py-2.5 text-sm gap-2',
}

export function Button({
  children,
  variant = 'primary',
  size = 'md',
  onClick,
  disabled = false,
  loading: externalLoading,
  type = 'button',
  className = '',
}: ButtonProps) {
  const [internalLoading, setInternalLoading] = useState(false)

  const isLoading = externalLoading ?? internalLoading

  const handleClick = useCallback(async () => {
    if (!onClick || isLoading || disabled) return
    const result = onClick()
    if (result instanceof Promise) {
      setInternalLoading(true)
      try {
        await result
      } finally {
        setInternalLoading(false)
      }
    }
  }, [onClick, isLoading, disabled])

  // Reset internal loading if disabled externally
  useEffect(() => {
    if (disabled) setInternalLoading(false)
  }, [disabled])

  return (
    <button
      type={type}
      onClick={handleClick}
      disabled={disabled || isLoading}
      className={`
        inline-flex items-center justify-center rounded-lg font-medium
        transition-all duration-150 cursor-pointer
        disabled:opacity-40 disabled:cursor-not-allowed disabled:pointer-events-none
        ${VARIANT_CLASSES[variant]}
        ${SIZE_CLASSES[size]}
        ${className}
      `}
    >
      {isLoading && <Loader2 className="w-3.5 h-3.5 animate-spin shrink-0" />}
      {children}
    </button>
  )
}
