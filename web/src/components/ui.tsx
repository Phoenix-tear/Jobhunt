import type { ReactNode } from 'react'
import type { JobSummary, Stage } from '../api/types'
import { STAGE_LABELS } from '../api/types'

const STAGE_STYLES: Record<Stage, string> = {
  saved: 'bg-stone-200 text-stone-800 dark:bg-stone-700 dark:text-stone-100',
  applied: 'bg-sky-100 text-sky-900 dark:bg-sky-900/60 dark:text-sky-100',
  oa_interview: 'bg-amber-100 text-amber-900 dark:bg-amber-900/60 dark:text-amber-100',
  offer: 'bg-emerald-100 text-emerald-900 dark:bg-emerald-900/60 dark:text-emerald-100',
  rejected: 'bg-rose-100 text-rose-900 dark:bg-rose-900/60 dark:text-rose-100',
}

export function StageBadge({ stage }: { stage: Stage }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${STAGE_STYLES[stage]}`}>
      {STAGE_LABELS[stage]}
    </span>
  )
}

export function Chip({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'brand' | 'warn' }) {
  const tones = {
    neutral: 'bg-stone-100 text-stone-700 dark:bg-stone-800 dark:text-stone-300',
    brand: 'bg-brand-100 text-brand-900 dark:bg-brand-900/50 dark:text-brand-100',
    warn: 'bg-amber-100 text-amber-900 dark:bg-amber-900/50 dark:text-amber-100',
  }
  return <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs ${tones[tone]}`}>{children}</span>
}

/** A small amber dot shown next to a job when it carries a caution-level posting signal. */
export function CautionDot({ show }: { show?: boolean }) {
  if (!show) return null
  return (
    <span
      role="img"
      aria-label="Has a posting caution signal"
      title="Posting caution: open the job to see the facts"
      className="inline-block size-2 shrink-0 rounded-full bg-amber-500"
    />
  )
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-sm text-stone-500">
      <span className="size-4 animate-spin rounded-full border-2 border-stone-300 border-t-brand-600" aria-hidden />
      {label}…
    </span>
  )
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-stone-300 px-6 py-12 text-center dark:border-stone-700">
      <p className="font-medium">{title}</p>
      {children ? <div className="mt-1 text-sm text-stone-500 dark:text-stone-400">{children}</div> : null}
    </div>
  )
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="rounded-lg border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-900 dark:border-rose-800 dark:bg-rose-950/50 dark:text-rose-100">
      <p className="font-medium">Something went wrong</p>
      <p className="mt-0.5 break-words">{message}</p>
      {onRetry ? (
        <button type="button" onClick={onRetry} className="mt-2 rounded border border-rose-400 px-2 py-1 text-xs font-medium hover:bg-rose-100 dark:hover:bg-rose-900/60">
          Try again
        </button>
      ) : null}
    </div>
  )
}

export function SkeletonRows({ count = 6 }: { count?: number }) {
  return (
    <ul aria-hidden className="space-y-px">
      {Array.from({ length: count }, (_, index) => (
        <li key={index} className="animate-pulse rounded-lg bg-white px-4 py-4 dark:bg-stone-900">
          <div className="h-4 w-2/3 rounded bg-stone-200 dark:bg-stone-800" />
          <div className="mt-2 h-3 w-1/3 rounded bg-stone-100 dark:bg-stone-800/70" />
        </li>
      ))}
    </ul>
  )
}

const LOCATION_BADGES: Record<string, [string, 'brand' | 'warn' | 'neutral']> = {
  india: ['India', 'brand'],
  remote_open_to_india: ['Remote · open to India', 'brand'],
  remote_unclear: ['Remote · check scope', 'warn'],
  abroad_unknown_workplace: ['Abroad', 'neutral'],
  wrong_location: ['Other location', 'neutral'],
}

/** Where the job is, per the location policy. `unknown` shows nothing. */
export function LocationBadge({ fit, reason, matched }: { fit: string | null; reason?: string | null; matched?: string | null }) {
  const entry = fit ? LOCATION_BADGES[fit] : undefined
  if (!entry) return null
  return (
    <span title={[reason, matched ? `matched: ${matched}` : null].filter(Boolean).join(' · ') || undefined}>
      <Chip tone={entry[1]}>{entry[0]}</Chip>
    </span>
  )
}

const SENIORITY_LABELS: Record<string, string> = { junior: 'Junior', caution: 'May be above entry level', mid: 'Mid-level', senior: 'Senior', executive: 'Executive' }

/** Rule-based seniority from the title; `unspecified` shows nothing (no signal either way). */
export function SeniorityBadge({ level, evidence }: { level: string | null; evidence?: string | null }) {
  if (!level || level === 'unspecified') return null
  const tone = level === 'junior' ? 'brand' : 'warn'
  return (
    <span title={evidence ?? undefined}>
      <Chip tone={tone}>{SENIORITY_LABELS[level] ?? level}</Chip>
    </span>
  )
}

const AUTO_HIDDEN_LABELS: Record<string, string> = { auto_stale: 'Auto-hidden: too old', closed_checked: 'Auto-hidden: posting closed' }

/** Age chip: how old the posting is, "age unknown" when there is no date, and why a job sits in Auto-hidden. */
export function FreshnessChip({ band, ageDays, autoHidden }: { band?: string; ageDays?: number | null; autoHidden?: string | null }) {
  if (autoHidden) return <Chip tone="warn">{AUTO_HIDDEN_LABELS[autoHidden] ?? 'Auto-hidden'}</Chip>
  if (band === 'unknown') return <span title="The posting has no date (or its source does not give one)"><Chip>age unknown</Chip></span>
  if (band === 'aging' || band === 'stale') return <span title="Posted a while ago: check that it is still open"><Chip tone="warn">{ageDays ?? '?'}d old</Chip></span>
  return null
}

/** 0-100 match score; the colour only helps scanning, the number is always shown. */
export function ScoreBadge({ score, llm }: { score: number | null; llm?: boolean }) {
  if (score === null) return null
  const tone =
    score >= 80
      ? 'bg-emerald-100 text-emerald-900 dark:bg-emerald-900/50 dark:text-emerald-100'
      : score >= 60
        ? 'bg-sky-100 text-sky-900 dark:bg-sky-900/50 dark:text-sky-100'
        : 'bg-stone-100 text-stone-700 dark:bg-stone-800 dark:text-stone-300'
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-semibold tabular-nums ${tone}`}
      title={llm ? 'Match score (includes an AI assessment)' : 'Match score (embedding similarity and location)'}
      aria-label={`Match score ${Math.round(score)}`}
    >
      {Math.round(score)}
    </span>
  )
}

/** Where the work happens and who a remote job is open to. */
export function RemoteChips({ job }: { job: Pick<JobSummary, 'workplace_type' | 'is_remote' | 'remote_eligible_india' | 'remote_scope'> }) {
  const remote = job.workplace_type === 'remote' || (job.workplace_type === 'unknown' && job.is_remote)
  return (
    <>
      {remote ? <Chip tone="brand">Remote</Chip> : null}
      {job.workplace_type === 'hybrid' ? <Chip>Hybrid</Chip> : null}
      {job.workplace_type === 'onsite' ? <Chip>On-site</Chip> : null}
      {remote && job.remote_eligible_india === true ? <Chip tone="brand">Open to India</Chip> : null}
      {remote && job.remote_eligible_india === false ? <Chip tone="warn">Not open to India</Chip> : null}
      {remote && job.remote_eligible_india === null && job.remote_scope === 'unknown' ? <Chip>Region unclear</Chip> : null}
    </>
  )
}
