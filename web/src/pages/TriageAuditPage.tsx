import React, { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useLabelAuditItem, useTriageAudit, useUndoAuditItem } from '../api/hooks'
import type { AuditItem } from '../api/types'

const CUE_REGEX =
  /\b(\d+\+?\s*(?:-\s*\d+\+?\s*)?(?:years?|yrs?)|US only|United States only|USA only|remote in US|remote - US|must reside in the US|must be based in the US|North America only|India|Bangalore|Bengaluru|Hyderabad|Pune|Mumbai|Delhi|Noida|Gurgaon|intern|internship|graduate|new grad|entry level|junior|associate|senior|lead|principal|staff|director|avp|vp)\b/gi

function renderHighlightedSnippet(snippet: string): React.ReactNode {
  if (!snippet) return <span className="italic text-stone-400">No snippet text</span>
  const parts: React.ReactNode[] = []
  let lastIndex = 0
  let match: RegExpExecArray | null

  CUE_REGEX.lastIndex = 0
  while ((match = CUE_REGEX.exec(snippet)) !== null) {
    if (match.index > lastIndex) {
      parts.push(snippet.slice(lastIndex, match.index))
    }
    parts.push(
      <mark
        key={match.index}
        className="rounded bg-amber-100 px-1 font-semibold text-amber-900 dark:bg-amber-900/50 dark:text-amber-200"
      >
        {match[0]}
      </mark>
    )
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < snippet.length) {
    parts.push(snippet.slice(lastIndex))
  }
  return <>{parts}</>
}

export function TriageAuditPage() {
  const { data: auditState, isLoading, error } = useTriageAudit()
  const labelMutation = useLabelAuditItem()
  const undoMutation = useUndoAuditItem()

  const [currentIndex, setCurrentIndex] = useState<number | null>(null)
  const [noteText, setNoteText] = useState('')

  // Sync index once loaded
  useEffect(() => {
    if (auditState && currentIndex === null) {
      setCurrentIndex(auditState.current_index)
    }
  }, [auditState, currentIndex])

  const items = auditState?.items ?? []
  const activeIndex = currentIndex ?? 0
  const currentItem: AuditItem | undefined = items[activeIndex]

  // Clear note input when moving to a new item, or populate with existing note
  useEffect(() => {
    if (currentItem) {
      setNoteText(currentItem.my_note || '')
    }
  }, [currentItem])

  const handleVote = (verdict: string) => {
    if (!currentItem) return
    labelMutation.mutate({
      jobId: currentItem.job_id,
      myVerdict: verdict,
      myNote: noteText.trim(),
    })
    if (activeIndex < items.length - 1) {
      setCurrentIndex(activeIndex + 1)
    }
  }

  const handleUndo = () => {
    if (!currentItem) return
    if (currentItem.my_verdict) {
      undoMutation.mutate(currentItem.job_id)
    } else if (activeIndex > 0) {
      const prevItem = items[activeIndex - 1]
      setCurrentIndex(activeIndex - 1)
      if (prevItem?.my_verdict) {
        undoMutation.mutate(prevItem.job_id)
      }
    }
  }

  const handlePrev = () => {
    if (activeIndex > 0) {
      setCurrentIndex(activeIndex - 1)
    }
  }

  const handleNext = () => {
    if (activeIndex < items.length - 1) {
      setCurrentIndex(activeIndex + 1)
    }
  }

  // Keyboard shortcut listener
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      // Ignore if user is currently focused on an input or textarea
      const tag = (e.target as HTMLElement)?.tagName?.toLowerCase()
      if (tag === 'input' || tag === 'textarea') return

      if (e.key === '1') {
        e.preventDefault()
        handleVote('local_right')
      } else if (e.key === '2') {
        e.preventDefault()
        handleVote('gemini_right')
      } else if (e.key === '3') {
        e.preventDefault()
        handleVote('both_wrong')
      } else if (e.key === '4') {
        e.preventDefault()
        handleVote('unsure')
      } else if (e.key === '5') {
        e.preventDefault()
        handleVote('both_right')
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault()
        handlePrev()
      } else if (e.key === 'ArrowRight') {
        e.preventDefault()
        handleNext()
      } else if (e.key.toLowerCase() === 'u') {
        e.preventDefault()
        handleUndo()
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  })

  const progressPct = useMemo(() => {
    if (!auditState || auditState.total_items === 0) return 0
    return Math.round((auditState.answered_count / auditState.total_items) * 100)
  }, [auditState])

  if (isLoading) {
    return (
      <div className="mx-auto max-w-4xl p-6 text-stone-600 dark:text-stone-300">
        Loading triage audit data...
      </div>
    )
  }

  if (error || !auditState) {
    return (
      <div className="mx-auto max-w-4xl p-6 text-red-600 dark:text-red-400">
        Failed to load audit items: {String(error)}
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-5xl space-y-6 px-4 py-6">
      {/* Top Header */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-stone-200 pb-4 dark:border-stone-800">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-xl font-bold tracking-tight text-stone-900 dark:text-stone-100">
              Triage Audit Review
            </h1>
            <span className="rounded bg-brand-100 px-2 py-0.5 text-xs font-semibold text-brand-800 dark:bg-brand-900/60 dark:text-brand-200">
              Clicks instead of CSV
            </span>
          </div>
          <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
            Calibrate model verdicts on disputed jobs. Labels use weight 5.0 and recompute acting rules instantly.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Link
            to="/health"
            className="rounded border border-stone-300 bg-white px-3 py-1.5 text-xs font-medium text-stone-700 shadow-sm hover:bg-stone-50 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-200"
          >
            ← Back to Health
          </Link>
          <Link
            to="/settings"
            className="rounded border border-stone-300 bg-white px-3 py-1.5 text-xs font-medium text-stone-700 shadow-sm hover:bg-stone-50 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-200"
          >
            Settings
          </Link>
        </div>
      </div>

      {/* Progress & Overview Bar */}
      <div className="rounded-lg border border-stone-200 bg-white p-4 shadow-sm dark:border-stone-800 dark:bg-stone-900">
        <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
          <div className="font-semibold text-stone-800 dark:text-stone-200">
            Row {activeIndex + 1} of {auditState.total_items}
          </div>
          <div className="text-xs text-stone-500 dark:text-stone-400">
            {auditState.answered_count} answered ({progressPct}%) ·{' '}
            <span className="text-emerald-600 dark:text-emerald-400">
              Gemini acc: {(auditState.gemini_accuracy * 100).toFixed(1)}% [
              {(auditState.gemini_ci[0] * 100).toFixed(0)}%–{(auditState.gemini_ci[1] * 100).toFixed(0)}%]
            </span>{' '}
            ·{' '}
            <span className="text-blue-600 dark:text-blue-400">
              Local acc: {(auditState.local_accuracy * 100).toFixed(1)}% [
              {(auditState.local_ci[0] * 100).toFixed(0)}%–{(auditState.local_ci[1] * 100).toFixed(0)}%]
            </span>
          </div>
        </div>
        <div className="mt-2.5 h-2 w-full overflow-hidden rounded-full bg-stone-100 dark:bg-stone-800">
          <div
            className="h-full bg-brand-600 transition-all duration-300"
            style={{ width: `${progressPct}%` }}
          />
        </div>
      </div>

      {/* Active Item Card */}
      {currentItem ? (
        <div className="space-y-4 rounded-xl border border-stone-200 bg-white p-6 shadow-sm dark:border-stone-800 dark:bg-stone-900">
          {/* Item Meta */}
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="flex items-center gap-2">
                <span className="text-xs font-semibold uppercase tracking-wider text-stone-500">
                  {currentItem.company}
                </span>
                <span className="rounded bg-stone-100 px-1.5 py-0.5 text-[11px] font-medium text-stone-600 dark:bg-stone-800 dark:text-stone-300">
                  via {currentItem.source}
                </span>
                <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[11px] font-medium text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300">
                  {currentItem.category}
                </span>
                {currentItem.my_verdict && (
                  <span className="rounded bg-emerald-100 px-2 py-0.5 text-[11px] font-semibold text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200">
                    Labeled: {currentItem.my_verdict}
                  </span>
                )}
              </div>
              <h2 className="mt-1 text-lg font-bold text-stone-900 dark:text-stone-100">
                {currentItem.title}
              </h2>
            </div>
            <a
              href={`/jobs?q=${encodeURIComponent(currentItem.company)}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 rounded border border-stone-300 bg-stone-50 px-2.5 py-1 text-xs font-medium text-stone-700 hover:bg-stone-100 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-300 dark:hover:bg-stone-700"
            >
              Open in Jobs ↗
            </a>
          </div>

          {/* Snippet with Highlighted Cues */}
          <div className="rounded-lg border border-stone-200 bg-stone-50 p-4 dark:border-stone-800 dark:bg-stone-950/60">
            <div className="mb-1 text-xs font-semibold uppercase tracking-wider text-stone-500">
              Posting Snippet (cue words highlighted)
            </div>
            <p className="text-sm leading-relaxed text-stone-800 dark:text-stone-200">
              {renderHighlightedSnippet(currentItem.snippet)}
            </p>
          </div>

          {/* Model Verdicts Comparison */}
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {/* Gemini */}
            <div className="rounded-lg border border-emerald-200 bg-emerald-50/40 p-4 dark:border-emerald-900/50 dark:bg-emerald-950/20">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold uppercase tracking-wider text-emerald-800 dark:text-emerald-300">
                  Gemini (Teacher)
                </span>
                <span className="rounded bg-emerald-100 px-2 py-0.5 text-xs font-semibold text-emerald-900 dark:bg-emerald-900 dark:text-emerald-100">
                  {currentItem.gemini_verdict}
                </span>
              </div>
              <div className="mt-2 text-xs italic text-stone-700 dark:text-stone-300">
                &ldquo;{currentItem.gemini_quote || 'No quote provided'}&rdquo;
              </div>
            </div>

            {/* Local LLM */}
            <div className="rounded-lg border border-blue-200 bg-blue-50/40 p-4 dark:border-blue-900/50 dark:bg-blue-950/20">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold uppercase tracking-wider text-blue-800 dark:text-blue-300">
                  Local LLM (Student)
                </span>
                <span className="rounded bg-blue-100 px-2 py-0.5 text-xs font-semibold text-blue-900 dark:bg-blue-900 dark:text-blue-100">
                  {currentItem.local_verdict}
                </span>
              </div>
              <div className="mt-2 text-xs italic text-stone-700 dark:text-stone-300">
                &ldquo;{currentItem.local_quote || 'No quote provided'}&rdquo;
              </div>
            </div>
          </div>

          {/* Deterministic Hint (Rule of Thumb) */}
          {currentItem.deterministic_hint && (
            <div className="flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50/60 p-3 text-xs text-amber-900 dark:border-amber-900/50 dark:bg-amber-950/20 dark:text-amber-200">
              <span className="text-base leading-none">💡</span>
              <div>
                <strong className="font-semibold">Deterministic hint (never auto-applied):</strong>{' '}
                {currentItem.deterministic_hint}
              </div>
            </div>
          )}

          {/* Optional Note */}
          <div>
            <label
              htmlFor="audit-note"
              className="block text-xs font-semibold uppercase tracking-wider text-stone-500"
            >
              Optional Note / Rationale
            </label>
            <input
              id="audit-note"
              type="text"
              placeholder="e.g. Explicit 5+ years requirement in requirements list..."
              value={noteText}
              onChange={(e) => setNoteText(e.target.value)}
              className="mt-1 w-full rounded-md border border-stone-300 bg-white px-3 py-1.5 text-sm text-stone-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-100"
            />
          </div>

          {/* Buttons: 1-5 Verdicts */}
          <div className="border-t border-stone-200 pt-4 dark:border-stone-800">
            <div className="mb-2 text-xs font-semibold text-stone-600 dark:text-stone-400">
              Record Verdict (Press 1–5 or click button):
            </div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
              <button
                type="button"
                onClick={() => handleVote('local_right')}
                className={`flex flex-col items-center justify-center rounded-lg border p-2.5 text-center transition-all ${
                  currentItem.my_verdict === 'local_right'
                    ? 'border-blue-600 bg-blue-50 ring-2 ring-blue-500 dark:bg-blue-950/50'
                    : 'border-stone-300 bg-white hover:bg-stone-50 dark:border-stone-700 dark:bg-stone-800 dark:hover:bg-stone-750'
                }`}
              >
                <span className="rounded bg-blue-100 px-1.5 py-0.5 text-xs font-bold text-blue-800 dark:bg-blue-900 dark:text-blue-200">
                  [1]
                </span>
                <span className="mt-1 text-xs font-medium text-stone-800 dark:text-stone-200">
                  Local Right
                </span>
              </button>

              <button
                type="button"
                onClick={() => handleVote('gemini_right')}
                className={`flex flex-col items-center justify-center rounded-lg border p-2.5 text-center transition-all ${
                  currentItem.my_verdict === 'gemini_right'
                    ? 'border-emerald-600 bg-emerald-50 ring-2 ring-emerald-500 dark:bg-emerald-950/50'
                    : 'border-stone-300 bg-white hover:bg-stone-50 dark:border-stone-700 dark:bg-stone-800 dark:hover:bg-stone-750'
                }`}
              >
                <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-xs font-bold text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200">
                  [2]
                </span>
                <span className="mt-1 text-xs font-medium text-stone-800 dark:text-stone-200">
                  Gemini Right
                </span>
              </button>

              <button
                type="button"
                onClick={() => handleVote('both_wrong')}
                className={`flex flex-col items-center justify-center rounded-lg border p-2.5 text-center transition-all ${
                  currentItem.my_verdict === 'both_wrong'
                    ? 'border-rose-600 bg-rose-50 ring-2 ring-rose-500 dark:bg-rose-950/50'
                    : 'border-stone-300 bg-white hover:bg-stone-50 dark:border-stone-700 dark:bg-stone-800 dark:hover:bg-stone-750'
                }`}
              >
                <span className="rounded bg-rose-100 px-1.5 py-0.5 text-xs font-bold text-rose-800 dark:bg-rose-900 dark:text-rose-200">
                  [3]
                </span>
                <span className="mt-1 text-xs font-medium text-stone-800 dark:text-stone-200">
                  Both Wrong
                </span>
              </button>

              <button
                type="button"
                onClick={() => handleVote('unsure')}
                className={`flex flex-col items-center justify-center rounded-lg border p-2.5 text-center transition-all ${
                  currentItem.my_verdict === 'unsure'
                    ? 'border-amber-600 bg-amber-50 ring-2 ring-amber-500 dark:bg-amber-950/50'
                    : 'border-stone-300 bg-white hover:bg-stone-50 dark:border-stone-700 dark:bg-stone-800 dark:hover:bg-stone-750'
                }`}
              >
                <span className="rounded bg-amber-100 px-1.5 py-0.5 text-xs font-bold text-amber-800 dark:bg-amber-900 dark:text-amber-200">
                  [4]
                </span>
                <span className="mt-1 text-xs font-medium text-stone-800 dark:text-stone-200">
                  Unsure
                </span>
              </button>

              <button
                type="button"
                onClick={() => handleVote('both_right')}
                className={`flex flex-col items-center justify-center rounded-lg border p-2.5 text-center transition-all ${
                  currentItem.my_verdict === 'both_right'
                    ? 'border-purple-600 bg-purple-50 ring-2 ring-purple-500 dark:bg-purple-950/50'
                    : 'border-stone-300 bg-white hover:bg-stone-50 dark:border-stone-700 dark:bg-stone-800 dark:hover:bg-stone-750'
                }`}
              >
                <span className="rounded bg-purple-100 px-1.5 py-0.5 text-xs font-bold text-purple-800 dark:bg-purple-900 dark:text-purple-200">
                  [5]
                </span>
                <span className="mt-1 text-xs font-medium text-stone-800 dark:text-stone-200">
                  Both Right
                </span>
              </button>
            </div>
          </div>

          {/* Navigation Controls: Back/Undo, Skip, Prev, Next */}
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-stone-200 pt-4 dark:border-stone-800">
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handlePrev}
                disabled={activeIndex === 0}
                className="rounded border border-stone-300 bg-white px-3 py-1.5 text-xs font-medium text-stone-700 disabled:opacity-40 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-300"
              >
                ← Previous
              </button>
              <button
                type="button"
                onClick={handleUndo}
                className="rounded border border-rose-300 bg-rose-50 px-3 py-1.5 text-xs font-semibold text-rose-700 hover:bg-rose-100 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-300"
              >
                Undo Verdict [U]
              </button>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handleNext}
                disabled={activeIndex >= items.length - 1}
                className="rounded border border-stone-300 bg-white px-3 py-1.5 text-xs font-medium text-stone-700 disabled:opacity-40 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-300"
              >
                Skip / Next →
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="rounded-lg border border-stone-200 bg-white p-8 text-center dark:border-stone-800 dark:bg-stone-900">
          <p className="text-stone-600 dark:text-stone-400">No audit items found in evaluation file.</p>
        </div>
      )}

      {/* Acting Rules Section */}
      <div className="rounded-xl border border-stone-200 bg-white p-6 shadow-sm dark:border-stone-800 dark:bg-stone-900">
        <h3 className="text-sm font-bold uppercase tracking-wider text-stone-900 dark:text-stone-100">
          Acting Rules ({auditState.acting_rules.length})
        </h3>
        <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
          Calibrated acting rules derived from human audit verdicts. Updated immediately on every vote.
        </p>

        {auditState.acting_rules.length > 0 ? (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-stone-200 bg-stone-50 text-stone-600 dark:border-stone-800 dark:bg-stone-800 dark:text-stone-300">
                <tr>
                  <th className="px-3 py-2">Field</th>
                  <th className="px-3 py-2">Condition</th>
                  <th className="px-3 py-2">Target</th>
                  <th className="px-3 py-2">Weight</th>
                  <th className="px-3 py-2">Sample</th>
                  <th className="px-3 py-2">Precision</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-stone-100 dark:divide-stone-800 text-stone-800 dark:text-stone-200">
                {auditState.acting_rules.map((rule, idx) => (
                  <tr key={idx} className="hover:bg-stone-50 dark:hover:bg-stone-850">
                    <td className="px-3 py-2 font-mono font-semibold">{rule.field}</td>
                    <td className="px-3 py-2 font-mono">
                      {rule.operator} {rule.threshold}
                    </td>
                    <td className="px-3 py-2 font-mono">{rule.target_class}</td>
                    <td className="px-3 py-2">{rule.weight}</td>
                    <td className="px-3 py-2">{rule.sample_size ?? '—'}</td>
                    <td className="px-3 py-2">
                      {rule.precision != null ? `${(rule.precision * 100).toFixed(0)}%` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="mt-3 text-xs text-stone-500">
            No acting rules active yet. Vote on conflicting audit items to generate calibrating rules.
          </div>
        )}
      </div>
    </div>
  )
}
