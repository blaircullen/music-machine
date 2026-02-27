import toast, { Toaster as HotToaster } from 'react-hot-toast'

export { toast }

export function Toaster() {
  return (
    <HotToaster
      position="bottom-right"
      toastOptions={{
        style: {
          background: '#1a1d27',
          color: '#e2e8f0',
          border: '1px solid rgba(255,255,255,0.08)',
          borderRadius: '12px',
          fontSize: '14px',
          fontFamily: 'inherit',
          boxShadow: '0 8px 32px rgba(0,0,0,0.5), 0 2px 8px rgba(0,0,0,0.3)',
        },
        success: {
          iconTheme: { primary: '#22c55e', secondary: '#1a1d27' },
        },
        error: {
          iconTheme: { primary: '#ef4444', secondary: '#1a1d27' },
        },
        duration: 4000,
      }}
    />
  )
}
