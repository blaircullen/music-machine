import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Toaster } from 'react-hot-toast'
import './index.css'
import App from './App'

const rootEl = document.getElementById('root')
if (!rootEl) throw new Error('No #root element found')

createRoot(rootEl).render(
  <StrictMode>
    <App />
    <Toaster
      position="bottom-right"
      toastOptions={{
        duration: 4000,
        style: {
          background: '#1a1d27',
          color: '#e2e8f0',
          border: '1px solid #2a2d3a',
          borderRadius: '10px',
          fontSize: '13px',
        },
        success: {
          iconTheme: { primary: '#22c55e', secondary: '#1a1d27' },
        },
        error: {
          iconTheme: { primary: '#ef4444', secondary: '#1a1d27' },
        },
      }}
    />
  </StrictMode>
)
