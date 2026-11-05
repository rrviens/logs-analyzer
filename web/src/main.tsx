import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { applyTextSize } from '@/components/Header'
import { lsGet } from '@/lib/wow'

applyTextSize(lsGet('textSize') === 'large')
// Visitor count: one ping per page load to our own server, which passes it to GoatCounter (ad blockers block
// GoatCounter's script). Path only; off unless the host has a token.
try {
  fetch('/api/pv', { method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
    body: JSON.stringify({ path: location.pathname, title: document.title,
                           ref: document.referrer && !document.referrer.startsWith(location.origin) ? document.referrer : '' }) })
} catch { /* counting is best-effort */ }

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
