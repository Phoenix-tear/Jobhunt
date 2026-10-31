import { useState } from 'react'
import { requestJson } from '../api/client'

export interface DemoBannerProps {
  onExit?: () => void
}

export function DemoBanner({ onExit }: DemoBannerProps) {
  const [loading, setLoading] = useState(false)

  const handleExit = async () => {
    setLoading(true)
    try {
      await requestJson('/api/setup/demo/exit', { method: 'POST' })
      if (onExit) {
        onExit()
      } else {
        window.location.href = '/'
      }
    } catch (err) {
      console.error('Failed to exit demo mode:', err)
      setLoading(false)
    }
  }

  return (
    <div
      role="banner"
      aria-label="Demo mode active"
      className="bg-amber-500 text-stone-950 px-4 py-2 text-sm font-medium flex items-center justify-between shadow-sm sticky top-0 z-40"
    >
      <div className="flex items-center gap-2">
        <span className="inline-flex items-center justify-center size-5 rounded-full bg-stone-950 text-amber-400 font-bold text-xs">
          !
        </span>
        <span>
          <strong>Demo Mode Active (Alex Chen Persona)</strong> &mdash; You are exploring an isolated sandbox with mock data. Real data is untouched and no live Gemini API calls are made ($0).
        </span>
      </div>
      <button
        type="button"
        onClick={handleExit}
        disabled={loading}
        className="rounded bg-stone-900 text-white px-3 py-1 text-xs font-semibold hover:bg-stone-800 transition-colors disabled:opacity-50 cursor-pointer"
      >
        {loading ? 'Exiting…' : 'Exit Demo Mode'}
      </button>
    </div>
  )
}
