import { useState } from 'react'
import { useSkillGaps } from '../api/hooks'
import type { TailoringStrength } from '../api/types'
import { Chip, ErrorState, Spinner } from './ui'

/** Low / Medium / High with the numbers on hover. "Unknown" when the comparison with the base was not recorded. */
export function StrengthChip({ strength }: { strength: TailoringStrength | undefined }) {
  if (!strength) return <span className="text-xs text-stone-500">–</span>
  const tone = strength.level === 'High' ? 'brand' : strength.level === 'Low' ? 'warn' : 'neutral'
  return (
    <span title={strength.text} data-testid="strength-chip">
      <Chip tone={tone}>{strength.level}</Chip>
      {strength.coverage != null ? <span className="ml-1 text-xs tabular-nums text-stone-500">{Math.round(strength.coverage * 100)}% JD</span> : null}
    </span>
  )
}

/** The honest explanation under a version: numbers, why it is Low, and what would help. */
export function StrengthDetail({ strength }: { strength: TailoringStrength }) {
  const parts = Object.entries(strength.parts)
  return (
    <div className="rounded border border-stone-200 p-2 text-sm dark:border-stone-700" aria-label="Tailoring strength">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-stone-500">Tailoring strength</h3>
        <StrengthChip strength={strength} />
        {strength.score != null ? <span className="text-xs tabular-nums text-stone-500">score {strength.score.toFixed(2)}</span> : null}
      </div>
      {parts.length > 0 ? (
        <p className="mt-1 text-xs text-stone-600 dark:text-stone-300">
          Different from the base resume in:{' '}
          {parts.map(([name, changed], index) => (
            <span key={name}>
              {index > 0 ? ' · ' : ''}
              {name} {changed ? '✓' : '–'}
            </span>
          ))}
        </p>
      ) : null}
      {strength.why.map((line, index) => (
        <p key={index} className="mt-1 text-xs text-amber-800 dark:text-amber-300">
          {line[0]?.toUpperCase()}
          {line.slice(1)}.
        </p>
      ))}
      {strength.help ? <p className="mt-1 text-xs text-stone-600 dark:text-stone-300">What would help: {strength.help}.</p> : null}
      {strength.level === 'Unknown' ? <p className="mt-1 text-xs text-stone-500">{strength.text}</p> : null}
    </div>
  )
}

/** Health: the job concepts my relevant jobs ask for that none of my ticked facts mention. Read-only, loaded on click. */
export function SkillGapsSection() {
  const [open, setOpen] = useState(false)
  const gaps = useSkillGaps(open)
  return (
    <section aria-label="Skill gaps" className="rounded-lg border border-stone-200 bg-white px-4 py-3 text-sm dark:border-stone-800 dark:bg-stone-900">
      <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-stone-500">Skill gaps across your relevant jobs</h2>
      <p className="mb-2 text-xs text-stone-600 dark:text-stone-300">
        The 25 concepts that your relevant open jobs ask for and none of your ticked facts mention. Read-only: nothing here is ever added to a resume. Use it to
        decide which real experience to add as facts.
      </p>
      {!open ? (
        <button type="button" onClick={() => setOpen(true)} className="rounded-md border border-stone-300 px-2.5 py-1 text-xs font-medium hover:bg-stone-100 dark:border-stone-600 dark:hover:bg-stone-800">
          Show skill gaps
        </button>
      ) : gaps.isPending ? (
        <Spinner label="Reading the postings" />
      ) : gaps.isError ? (
        <ErrorState message={gaps.error.message} onRetry={() => void gaps.refetch()} />
      ) : (
        <div>
          <p className="mb-2 text-xs text-stone-500">
            {gaps.data.jobs_checked.toLocaleString()} relevant open jobs checked (best-scored first, at most {gaps.data.job_limit.toLocaleString()}).
          </p>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-left text-sm">
              <thead>
                <tr className="text-xs uppercase tracking-wide text-stone-500">
                  <th className="py-1 pr-3 font-medium">Concept</th>
                  <th className="py-1 pr-3 font-medium">Jobs asking</th>
                  <th className="py-1 font-medium">Example titles</th>
                </tr>
              </thead>
              <tbody>
                {gaps.data.items.map((item) => (
                  <tr key={item.concept} className="border-t border-stone-100 align-top dark:border-stone-800">
                    <td className="py-1 pr-3 font-medium">{item.label}</td>
                    <td className="py-1 pr-3 tabular-nums">{item.jobs}</td>
                    <td className="py-1 text-xs text-stone-600 dark:text-stone-300">{item.examples.join(' · ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  )
}
