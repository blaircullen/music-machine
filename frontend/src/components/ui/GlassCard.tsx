import { motion } from 'motion/react'
import type { ReactNode } from 'react'

interface GlassCardProps {
  children: ReactNode
  className?: string
  hover?: boolean
}

export function GlassCard({ children, className = '', hover = false }: GlassCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className={`bg-glass border border-glass-border rounded-2xl shadow-lg backdrop-blur-xl ${hover ? 'hover:bg-glass-hover hover:border-base-400/30 hover:shadow-lime-glow transition-all duration-300' : ''} ${className}`}
    >
      {children}
    </motion.div>
  )
}
