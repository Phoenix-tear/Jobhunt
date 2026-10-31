import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMarkOpened, useMarkSeen, useTailorJob, useToday } from '../api/hooks'
import type { TodayJob } from '../api/types'
import { DecisionControls } from '../components/DecisionDialogs'
import { useToast } from '../components/Toast'
import { CautionDot, Chip, EmptyState, ErrorState, LocationBadge, ScoreBadge, SeniorityBadge, Spinner } from '../components/ui'
import { formatDate, sourceLabel, timeAgo } from '../lib/format'
import { isSafeHttpUrl } from '../components/JobDetailPanel'

const buttonClass =
  'rounded-md border border-stone-300 px-2 py-1 text-xs font-medium hover:bg-stone-100 disabled:opacity-50 dark:border-stone-600 dark:hover:bg-stone-800'

interface RowProps {
  job: TodayJob
  /** Extra line under the title (deadline, "closed after you opened it", ...). */
  note?: string
  showFirstSeen?: boolean
}

function TodayRow({ job, note, showFirstSeen = false }: RowProps) {
  const opened = useMarkOpened()
  const tailor = useTailorJob()
  const { notify } = useToast()

  return (
    <li className="rounded-lg border border-stone-200 bg-white px-3 py-2.5 dark:border-stone-800 dark:bg-stone-900" data-job-id={job.id}>
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1">
          <Link to={`/jobs?job=${job.id}`} className="block truncate font-medium hover:underline" title={job.title}>
            {job.title}
            <span className="ml-1.5 inline-block align-middle">
              <CautionDot show={job.has_caution_signal} />
            </span>
          </Link>
          <p className="truncate text-xs text-stone-500">
            {job.company_name || 'Unknown company'}
            {job.location ? ` · ${job.location}` : ''}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {job.likely_ineligible ? <Chip tone="warn">{job.likely_ineligible}</Chip> : null}
          {job.applied_company_warning ? <Chip tone="warn">{job.applied_company_warning}</Chip> : null}
          <ScoreBadge score={job.match_score} llm={job.llm_scored} />
          <LocationBadge fit={job.location_fit} reason={job.location_reason} matched={job.location_matched} />
          <SeniorityBadge level={job.seniority_level} evidence={job.seniority_evidence} />
          {job.sources.map((source) => (
            <Chip key={source}>{sourceLabel(source)}</Chip>
          ))}
          {showFirstSeen ? <Chip>first seen {timeAgo(job.first_seen_at)}</Chip> : null}
          {job.opened_at ? <Chip tone="brand">opened {timeAgo(job.opened_at)}</Chip> : null}
        </div>
      </div>
      {note ? <p className="mt-1 text-xs text-stone-600 dark:text-stone-300">{note}</p> : null}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {isSafeHttpUrl(job.apply_url) ? (
          <a
            href={job.apply_url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={() => opened.mutate(job.id)}
            className="rounded-md bg-brand-700 px-2 py-1 text-xs font-medium text-white hover:bg-brand-800"
          >
            Open apply page ↗
          </a>
        ) : (
          <span className="text-xs text-stone-400">No apply link</span>
        )}
        <button
          type="button"
          className={buttonClass}
          disabled={tailor.isPending}
          onClick={() =>
            tailor.mutate(
              { jobId: job.id },
              {
                onSuccess: (result) =>
                  notify(
                    result.status === 'failed'
                      ? `Tailoring “${job.title}” failed: ${result.message ?? 'see the job panel'}`
                      : `Tailored a resume for “${job.title}” (${result.mode}, ${result.pages} page).`,
                    result.status === 'failed' ? 'error' : 'info',
                  ),
                onError: (error) => notify(`Could not tailor: ${error.message}`, 'error'),
              },
            )
          }
        >
          {tailor.isPending ? 'Tailoring…' : 'Tailor'}
        </button>
        <DecisionControls job={job} compact />
      </div>
    </li>
  )
}

function Section({
  title,
  count,
  hint,
  children,
}: {
  title: string
  count: number
  hint?: string
  children: React.ReactNode
}) {
  return (
    <section aria-label={title} className="space-y-2">
      <div className="flex items-baseline gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-stone-500">{title}</h2>
        <span className="text-xs text-stone-400">{count}</span>
        {hint ? <span className="text-xs text-stone-400">· {hint}</span> : null}
      </div>
      {count === 0 ? <p className="rounded-lg border border-dashed border-stone-300 px-3 py-3 text-sm text-stone-500 dark:border-stone-700">Nothing here.</p> : <ul className="space-y-2">{children}</ul>}
    </section>
  )
}

export function TodayPage() {
  const today = useToday()
  const seen = useMarkSeen()
  const [hideCaution, setHideCaution] = useState(false)
  const [showIneligible, setShowIneligible] = useState(false) // likely-ineligible jobs are hidden unless asked for
  const raw = today.data
  const keep = (jobs: TodayJob[]) =>
    jobs.filter((job) => (!hideCaution || !job.has_caution_signal) && (showIneligible || !job.likely_ineligible))
  const data = raw
    ? {
        ...raw,
        new_total: Math.max(0, raw.new_total - (raw.new_jobs.length - keep(raw.new_jobs).length)),
        new_jobs: keep(raw.new_jobs),
        top: keep(raw.top),
        closing_soon: keep(raw.closing_soon),
        approved_not_applied: keep(raw.approved_not_applied),
      }
    : undefined

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold tracking-tight">Today</h1>
        {data ? (
          <p className="flex flex-wrap items-center gap-x-3 text-xs text-stone-500">
            <label className="inline-flex items-center gap-1.5">
              <input type="checkbox" checked={!showIneligible} onChange={(event) => setShowIneligible(!event.target.checked)} />
              Hide likely-ineligible jobs
            </label>
            <label className="inline-flex items-center gap-1.5">
              <input type="checkbox" checked={hideCaution} onChange={(event) => setHideCaution(event.target.checked)} />
              Hide jobs with a posting caution
            </label>
            <span>
            Since your last visit ({timeAgo(data.since)}) ·{' '}
            <button type="button" className="underline" onClick={() => seen.mutate()}>
              Mark everything seen
            </button>
            </span>
          </p>
        ) : null}
      </div>

      {today.isPending ? <Spinner label="Building your day" /> : null}
      {today.isError ? <ErrorState message={today.error.message} onRetry={() => void today.refetch()} /> : null}

      {data ? (
        <>
          {data.week ? (
            <p aria-label="This week" className="rounded-lg border border-stone-200 bg-white px-3 py-2 text-sm dark:border-stone-800 dark:bg-stone-900">
              <strong>This week:</strong> {data.week.applied} applied, {data.week.saved} saved, {data.week.not_applying} not applying, follow-ups due {data.week.follow_ups_due}
            </p>
          ) : null}
          <section aria-label="Counts per source" className="flex flex-wrap gap-2">
            {data.sources.length === 0 ? (
              <EmptyState title="No sources have produced jobs yet">Run an ingest, or wait for the scheduled one.</EmptyState>
            ) : (
              data.sources.map((source) => (
                <span
                  key={source.source}
                  className="rounded-lg border border-stone-200 bg-white px-3 py-1.5 text-xs dark:border-stone-800 dark:bg-stone-900"
                  title={`${source.open_total.toLocaleString()} open in total`}
                >
                  <strong>{sourceLabel(source.source)}</strong>{' '}
                  <span className="tabular-nums">
                    {source.new_today} new today · {source.relevant_today} relevant
                  </span>
                </span>
              ))
            )}
          </section>

          <div className="grid gap-6 xl:grid-cols-2">
            <div className="space-y-6">
              <Section
                title="New relevant jobs"
                count={data.new_total}
                hint={data.new_total > data.new_jobs.length ? `showing ${data.new_jobs.length}, best score first` : 'best score first'}
              >
                {data.new_jobs.map((job) => (
                  <TodayRow key={job.id} job={job} showFirstSeen />
                ))}
              </Section>
              <Section title="Top 15 by score" count={data.top.length} hint="not dismissed, not applied">
                {data.top.map((job) => (
                  <TodayRow key={job.id} job={job} />
                ))}
              </Section>
            </div>
            <div className="space-y-6">
              <Section title="Follow up" count={data.follow_ups.length} hint="applied 7+ days ago with no status change">
                {data.follow_ups.map((job) => (
                  <TodayRow
                    key={job.id}
                    job={job}
                    note={`Applied ${job.applied_at ? formatDate(job.applied_at) : 'earlier'} · ${job.days_since_change ?? 0} days without a change${
                      job.follow_up_at ? ` · reminder ${formatDate(job.follow_up_at)}` : ''
                    }${job.contact_name ? ` · contact ${job.contact_name}` : ''}`}
                  />
                ))}
              </Section>
              <Section title="Interviews coming up" count={data.interviews.length}>
                {data.interviews.map((job) => (
                  <TodayRow
                    key={job.id}
                    job={job}
                    note={job.next_date ? `Next date ${formatDate(job.next_date)}` : 'No date set: add one in the tracker'}
                  />
                ))}
              </Section>
              <Section title="Resume approved, not applied" count={data.approved_not_applied.length}>
                {data.approved_not_applied.map((job) => (
                  <TodayRow key={job.id} job={job} note="You approved a tailored resume for this job but have not applied yet." />
                ))}
              </Section>
              <Section title="Closing soon" count={data.closing_soon.length} hint="only where the posting states a deadline">
                {data.closing_soon.map((job) => (
                  <TodayRow key={job.id} job={job} note={job.deadline_at ? `Closes ${formatDate(job.deadline_at)}` : undefined} />
                ))}
              </Section>
              <Section title="Disappeared since you opened them" count={data.disappeared.length}>
                {data.disappeared.map((job) => (
                  <TodayRow
                    key={job.id}
                    job={job}
                    note={`Closed ${job.closed_at ? timeAgo(job.closed_at) : 'recently'} at the source${
                      job.opened_at ? `; you opened it ${timeAgo(job.opened_at)}` : ''
                    }.`}
                  />
                ))}
              </Section>
            </div>
          </div>
        </>
      ) : null}
    </div>
  )
}
