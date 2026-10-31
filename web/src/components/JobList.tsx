import type { JobSummary } from '../api/types'
import { LEVEL_LABELS, TYPE_LABELS, sourceLabel, timeAgo } from '../lib/format'
import { DecisionControls } from './DecisionDialogs'
import { useRestoreJob } from '../api/hooks'
import { CautionDot, Chip, FreshnessChip, LocationBadge, RemoteChips, ScoreBadge, SeniorityBadge, StageBadge } from './ui'

interface Props {
  jobs: JobSummary[]
  selectedId: number | null
  onSelect: (id: number) => void
  /** Rows ticked for a bulk action (Not applying with one reason). */
  checkedIds?: ReadonlySet<number>
  onToggleChecked?: (id: number) => void
}

export function JobList({ jobs, selectedId, onSelect, checkedIds, onToggleChecked }: Props) {
  const restore = useRestoreJob()
  return (
    <ul aria-label="Jobs" className="divide-y divide-stone-200 overflow-hidden rounded-lg border border-stone-200 bg-white dark:divide-stone-800 dark:border-stone-800 dark:bg-stone-900">
      {jobs.map((job) => {
        const selected = job.id === selectedId
        const where = [job.city, job.country].filter(Boolean).join(', ') || job.location
        return (
          <li key={job.id} data-job-id={job.id} className={selected ? 'bg-brand-50 dark:bg-brand-900/30' : ''}>
            <div className="flex items-start">
              {onToggleChecked ? (
                <label className="flex items-center self-stretch px-3">
                  <input
                    type="checkbox"
                    aria-label={`Select ${job.title}`}
                    checked={checkedIds?.has(job.id) ?? false}
                    onChange={() => onToggleChecked(job.id)}
                  />
                </label>
              ) : null}
              <button
                type="button"
                onClick={() => onSelect(job.id)}
                aria-current={selected ? 'true' : undefined}
                className="block min-w-0 flex-1 px-4 py-3 text-left transition-colors hover:bg-stone-50 dark:hover:bg-stone-800/60"
              >
                <span className="flex items-start gap-3">
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="block truncate font-medium">{job.title}</span>
                      <CautionDot show={job.has_caution_signal} />
                    </span>
                    <span className="mt-0.5 block truncate text-sm text-stone-600 dark:text-stone-400">
                      {job.company_name || 'Unknown company'}
                      {where ? <span className="text-stone-400"> · {where}</span> : null}
                    </span>
                    <span className="mt-1.5 flex flex-wrap items-center gap-1">
                      {job.likely_ineligible ? <Chip tone="warn">{job.likely_ineligible}</Chip> : null}
                      {job.applied_company_warning ? <Chip tone="warn">{job.applied_company_warning}</Chip> : null}
                      {(job.learned_rules ?? []).map((rule) => (
                        <span key={rule.id ?? rule.phrase} title={rule.tooltip ?? ''}>
                          <Chip>learned rule</Chip>
                        </span>
                      ))}
                      <LocationBadge fit={job.location_fit} reason={job.location_reason} matched={job.location_matched} />
                      <SeniorityBadge level={job.seniority_level} evidence={job.seniority_evidence} />
                      <RemoteChips job={job} />
                      {job.work_auth_required ? <Chip tone="warn">Work authorization</Chip> : null}
                      {job.feedback === 'dismiss' && !job.decision ? <Chip>Dismissed</Chip> : null}
                      {job.experience_level !== 'unknown' ? <Chip>{LEVEL_LABELS[job.experience_level]}</Chip> : null}
                      <Chip>{TYPE_LABELS[job.employment_type]}</Chip>
                      {job.needs_description ? <Chip tone="warn">No description yet</Chip> : null}
                      {job.status === 'closed' ? <Chip tone="warn">Closed</Chip> : null}
                      <FreshnessChip band={job.freshness} ageDays={job.age_days} autoHidden={job.auto_hidden} />
                      {job.sources.slice(0, 3).map((source) => (
                        <span key={source} className="text-xs text-stone-400">
                          {sourceLabel(source)}
                        </span>
                      ))}
                    </span>
                    {job.exclusion_reason ? (
                      <span className="mt-1 block text-xs text-stone-500 dark:text-stone-400">Filtered out: {job.exclusion_reason}</span>
                    ) : null}
                    {job.location_fit === 'wrong_location' && job.location_reason ? (
                      <span className="mt-1 block text-xs text-stone-500 dark:text-stone-400">Location: {job.location_reason}</span>
                    ) : null}
                  </span>
                  <span className="flex shrink-0 flex-col items-end gap-1.5 text-xs text-stone-500">
                    <ScoreBadge score={job.match_score} llm={job.llm_scored} />
                    <time dateTime={job.listed_at}>{timeAgo(job.listed_at)}</time>
                    {job.stage ? <StageBadge stage={job.stage} /> : null}
                  </span>
                </span>
              </button>
            </div>
            <div className="flex flex-wrap items-center gap-2 px-4 pb-2.5 pl-4">
              <DecisionControls job={job} compact />
              {job.auto_hidden ? (
                <button
                  type="button"
                  disabled={restore.isPending}
                  onClick={() => restore.mutate(job.id)}
                  className="rounded-md border border-stone-300 px-2 py-0.5 text-xs font-medium hover:bg-stone-100 dark:border-stone-600 dark:hover:bg-stone-800"
                >
                  Restore
                </button>
              ) : null}
            </div>
          </li>
        )
      })}
    </ul>
  )
}
