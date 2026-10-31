import { useEffect, useRef } from 'react'
import { useCheckOpen, useJob, useRestoreJob, useUndoVerdictWrong, useVerdictWrong } from '../api/hooks'
import { VERDICT_WRONG_LABELS } from '../api/types'
import type { JobDetail } from '../api/types'
import { useState } from 'react'
import { LEVEL_LABELS, TYPE_LABELS, formatDate, formatSalary, sourceLabel, timeAgo } from '../lib/format'
import { DecisionControls } from './DecisionDialogs'
import { ResumeBox } from './ResumeBox'
import { WritingSection } from './WritingSection'
import { Chip, ErrorState, FreshnessChip, LocationBadge, RemoteChips, ScoreBadge, SeniorityBadge, Spinner } from './ui'

interface Props {
  jobId: number
  onClose: () => void
}

/** Only ever link to plain http(s) URLs (the API already filters; this is defense in depth). */
export function isSafeHttpUrl(value: string | null | undefined): value is string {
  if (!value) return false
  try {
    const url = new URL(value)
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

const LABELS: Record<string, string> = {
  fit: 'Fit',
  similarity_percentile: 'Similarity rank',
  location_score: 'Location',
  family_points: 'Role-family adjustment',
  years_points: 'Experience penalty',
}

const LEVEL_WORDS: Record<string, string> = {
  intern: 'intern', entry: 'entry level', mid: 'mid level', senior: 'senior', lead_or_manager: 'lead or manager', unknown: 'unknown',
}
const USED_BY: Record<string, string> = {
  llm_seniority: 'Excluded by this verdict (level or required years)',
  llm_seniority_caution: 'Capped at Maybe by this verdict',
  llm_location: 'Hidden by location because of this verdict',
  eligibility_flag: 'Its blocker became an eligibility flag',
}

/** The LLM verdict with the exact words it relied on, which rule used it, and the "verdict is wrong" button. */
function LlmVerdict({ job }: { job: JobDetail }) {
  const triage = job.triage
  const wrong = useVerdictWrong()
  const undo = useUndoVerdictWrong()
  const [label, setLabel] = useState<string>('wrong_level')
  if (!triage) return null
  const verdict = triage.verdict
  const student = triage.student
  return (
    <div aria-label="LLM verdict" role="group" className="mt-3 rounded-md border border-stone-200 px-3 py-2 text-sm dark:border-stone-700">
      <div className="flex items-center justify-between">
        <p className="text-xs font-semibold uppercase tracking-wide text-stone-500">LLM verdict</p>
        {verdict ? (
          <div className="flex items-center gap-1.5">
            <span className="rounded bg-stone-100 px-1.5 py-0.5 text-[10px] font-mono dark:bg-stone-800">
              {verdict.source === 'gemini' ? 'Gemini' : 'Local LLM'}
            </span>
            <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${verdict.acted_or_advised === 'acted' ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200' : 'bg-stone-100 text-stone-600 dark:bg-stone-800 dark:text-stone-400'}`}>
              {verdict.acted_or_advised === 'acted' ? 'Acted' : 'Advised'}
            </span>
            {verdict.company_precedence_applied ? (
              <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] font-semibold text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300" data-testid="company-precedence-chip">
                company rule allows this grade
              </span>
            ) : null}
            {verdict.models_disagree ? (
              <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800 dark:bg-amber-950 dark:text-amber-200" data-testid="models-disagree-chip">
                models disagree
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
      {verdict ? (
        <>
          <p className="mt-1" data-testid="verdict-summary">
            {LEVEL_WORDS[verdict.level] ?? verdict.level}
            {verdict.required_years_min !== null ? `, asks ${verdict.required_years_min}+ years` : ', no years stated'}
            {' · '}scope: {verdict.location_scope || 'unclear'} · open to India: {verdict.india_eligible} · confidence {verdict.confidence}
          </p>
          {verdict.strength_reason ? (
            <p className="mt-1 text-xs text-stone-600 dark:text-stone-300" data-testid="verdict-strength">
              <span className="font-medium text-stone-500">Strength reason:</span> {verdict.strength_reason}
            </p>
          ) : null}
          {verdict.quotes.length > 0 ? (
            <ul aria-label="Verdict evidence" className="mt-1 space-y-0.5 text-xs text-stone-600 dark:text-stone-300">
              {verdict.quotes.map((item) => (
                <li key={item.field}>
                  <span className="font-medium">{item.field}</span> ({item.value}): <span className="italic">“{item.quote}”</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-1 text-xs text-stone-500">No quote could be verified, so nothing here changes this job.</p>
          )}
          {verdict.unverified_fields.length > 0 ? (
            <p className="mt-1 text-xs text-amber-800 dark:text-amber-300">
              Dropped, quote not found in the posting: {verdict.unverified_fields.join(', ')}.
            </p>
          ) : null}
          <p className="mt-1 text-xs" data-testid="verdict-used">
            <strong>{USED_BY[verdict.used_by] ?? 'Not used'}</strong>
            {verdict.used_text ? `: ${verdict.used_text}` : ''}
          </p>
          <p className="mt-1 text-[11px] text-stone-400">
            {verdict.model} · rubric {verdict.rubric_version}
          </p>
          {verdict.human_label ? (
            <p className="mt-2 flex flex-wrap items-center gap-2 text-xs">
              <span>You marked this verdict wrong ({verdict.human_label.replace('_', ' ')}); it is ignored for this job.</span>
              <button type="button" disabled={undo.isPending} onClick={() => undo.mutate(job.id)} className="rounded border border-stone-300 px-2 py-0.5 hover:bg-stone-100 dark:border-stone-600 dark:hover:bg-stone-800">
                Use the verdict again
              </button>
            </p>
          ) : (
            <p className="mt-2 flex flex-wrap items-center gap-2 text-xs">
              <select aria-label="What was wrong" value={label} onChange={(event) => setLabel(event.target.value)} className="rounded border border-stone-300 bg-transparent px-1 py-0.5 dark:border-stone-600">
                {VERDICT_WRONG_LABELS.map(([value, text]) => (
                  <option key={value} value={value}>{text}</option>
                ))}
              </select>
              <button type="button" disabled={wrong.isPending} onClick={() => wrong.mutate({ jobId: job.id, label })} className="rounded border border-stone-300 px-2 py-0.5 hover:bg-stone-100 dark:border-stone-600 dark:hover:bg-stone-800">
                LLM verdict is wrong
              </button>
            </p>
          )}
          {wrong.isError ? <p role="alert" className="mt-1 text-xs text-red-700">{wrong.error.message}</p> : null}
        </>
      ) : null}
      {student && (student.rules?.length || student.p_senior != null || student.p_not_india != null || student.chips?.length) ? (
        <p className="mt-2 text-xs text-stone-600 dark:text-stone-300" data-testid="student-chips">
          {student.rules?.map((rule) => (
            <Chip key={rule.rule}>rule: {rule.rule}{rule.precision != null ? ` (${Math.round(rule.precision * 100)}% precise)` : ''}</Chip>
          ))}
          {student.p_senior != null ? <Chip>senior {Math.round(student.p_senior * 100)}%</Chip> : null}
          {student.p_not_india != null ? <Chip>not India {Math.round(student.p_not_india * 100)}%</Chip> : null}
          {student.chips?.map((c) => <Chip key={c}>{c}</Chip>)}
          {student.demoted_by ? <span className="ml-1">demoted by {student.demoted_by.replace('_', ' ')}</span> : null}
        </p>
      ) : null}
      {triage.cascade && triage.cascade.length > 0 ? (
        <div className="mt-2 border-t border-stone-100 pt-2 text-xs dark:border-stone-800" data-testid="cascade-decisions">
          <p className="font-semibold text-stone-500 uppercase tracking-wide text-[10px]">Cascade Decisions</p>
          <ul className="mt-1 space-y-1">
            {triage.cascade.map((c, i) => (
              <li key={`${c.head}-${i}`} className="flex flex-wrap items-center gap-1.5 text-stone-600 dark:text-stone-300">
                <span className="font-medium capitalize">{c.head}:</span>
                <span className="rounded bg-stone-100 px-1 py-0.5 text-[10px] font-mono dark:bg-stone-800">
                  {c.tier === 0 ? 'Tier 0: Rules' : c.tier === 1 ? 'Tier 1: Classifier' : c.tier === 2 ? 'Tier 2: Local LLM' : 'Tier 3: Gemini'}
                </span>
                <span className={`font-semibold ${c.action === 'demote' ? 'text-amber-700 dark:text-amber-400' : 'text-stone-500'}`}>
                  {c.action}
                </span>
                {c.confidence != null ? <span>(p={c.confidence.toFixed(2)})</span> : null}
                {c.evidence ? <span className="italic text-stone-400 truncate max-w-xs">“{c.evidence}”</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  )
}

function WhyThisMatch({ job }: { job: JobDetail }) {
  const match = job.match
  if (!match) return null
  const why = job.why_match
  const hidden = match.gate_status === 'excluded'
  const components = match.components
  const rows = Object.entries(LABELS).filter(([key]) => typeof components[key] === 'number')
  const flags = job.eligibility ?? []
  return (
    <section aria-label="Why this match" className="rounded-lg border border-stone-200 px-4 py-3 dark:border-stone-800">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-stone-500">Why this match</h3>
        <ScoreBadge score={match.final_score} llm={match.llm_score !== null} />
      </div>
      {hidden ? (
        <p className="mt-2 text-sm">
          <span className="font-medium">Filtered out:</span> {match.exclusion_reason}
          {match.gate_rule ? <span className="ml-1 text-xs text-stone-400">({match.gate_rule})</span> : null}
        </p>
      ) : null}
      {match.reason ? (
        <p className="mt-2 text-sm">{match.reason}</p>
      ) : (
        <p className="mt-2 text-sm text-stone-600 dark:text-stone-300">
          No Gemini reason stored for this job.{why?.fit_source_note ? ` Fit ${Math.round(Number(why.fit ?? 0))}: ${why.fit_source_note}.` : ''}
        </p>
      )}
      {flags.length > 0 ? (
        <ul aria-label="Eligibility flags" className="mt-2 space-y-1 text-sm">
          {flags.map((flag) => (
            <li key={flag.kind} className={flag.strength === 'required' ? 'text-amber-900 dark:text-amber-200' : 'text-stone-600 dark:text-stone-300'}>
              <span className="font-medium">{flag.strength === 'required' ? flag.label : `${flag.label} (preferred / information)`}</span>
              <span className="block text-xs italic">posting says: “{flag.snippet}”</span>
            </li>
          ))}
        </ul>
      ) : null}
      {why && (why.similarity_ai !== null || why.similarity_fullstack !== null) ? (
        <p className="mt-2 text-sm" data-testid="profile-similarity">
          Resume similarity: AI/ML <strong>{why.similarity_ai?.toFixed(3) ?? '–'}</strong> · Full-stack{' '}
          <strong>{why.similarity_fullstack?.toFixed(3) ?? '–'}</strong>
          {why.profile_note ? <span className="block text-xs text-stone-500">{why.profile_note}.</span> : null}
        </p>
      ) : null}
      {match.recommended_profile ? (
        <p className="mt-2 text-sm">
          Best resume: <strong>{match.recommended_profile === 'ai' ? 'AI / ML profile' : 'Full-stack profile'}</strong>
        </p>
      ) : null}
      {match.matched_skills.length > 0 ? (
        <p className="mt-2 flex flex-wrap items-center gap-1 text-sm">
          <span className="text-stone-500">Matched:</span>
          {match.matched_skills.map((skill) => (
            <Chip key={skill} tone="brand">
              {skill}
            </Chip>
          ))}
        </p>
      ) : null}
      {match.missing_skills.length > 0 ? (
        <p className="mt-1.5 flex flex-wrap items-center gap-1 text-sm">
          <span className="text-stone-500">Missing:</span>
          {match.missing_skills.map((skill) => (
            <Chip key={skill}>{skill}</Chip>
          ))}
        </p>
      ) : null}
      {match.red_flags.length > 0 ? (
        <ul className="mt-2 space-y-0.5 text-sm text-amber-900 dark:text-amber-200">
          {match.red_flags.map((flag) => (
            <li key={flag}>⚠ {flag}</li>
          ))}
        </ul>
      ) : null}
      {(job.learned_rules ?? []).length > 0 ? (
        <p className="mt-2 flex flex-wrap items-center gap-1 text-xs">
          {(job.learned_rules ?? []).map((rule) => (
            <span key={rule.id ?? rule.phrase} title={rule.tooltip ?? ''}>
              <Chip>learned rule {rule.id}: {rule.phrase}</Chip>
            </span>
          ))}
        </p>
      ) : null}
      {rows.length > 0 ? (
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs text-stone-500">
          {rows.map(([key, label]) => (
            <div key={key} className="contents">
              <dt>{label}</dt>
              <dd className="tabular-nums">{Math.round(Number(components[key]))}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {why?.formula ? <p className="mt-2 text-[11px] leading-snug text-stone-400" data-testid="score-formula">{why.formula}.</p> : null}
      {match.remote_evidence ? <p className="mt-2 text-xs text-stone-500">Workplace evidence: {match.remote_evidence}</p> : null}
      <LlmVerdict job={job} />
    </section>
  )
}

const LIVENESS_TEXT: Record<string, string> = {
  open: 'Still open',
  closed: 'Closed: no longer available',
  uncertain: 'Could not tell (the site did not answer clearly); nothing was changed',
}

/** "Check if still open": one polite request, only on an explicit click; shows the answer and when it was checked. */
function OpenCheck({ job }: { job: JobDetail }) {
  const check = useCheckOpen()
  const restore = useRestoreJob()
  const last = check.data ?? job.liveness ?? null
  return (
    <section aria-label="Still open?" className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-sm">
      <button
        type="button"
        disabled={check.isPending}
        onClick={() => check.mutate(job.id)}
        className="rounded-md border border-stone-300 px-3 py-1.5 text-sm font-medium hover:bg-stone-100 disabled:opacity-60 dark:border-stone-600 dark:hover:bg-stone-800"
      >
        {check.isPending ? 'Checking…' : 'Check if still open'}
      </button>
      {job.auto_hidden ? (
        <button
          type="button"
          disabled={restore.isPending}
          onClick={() => restore.mutate(job.id)}
          className="rounded-md border border-stone-300 px-3 py-1.5 text-sm font-medium hover:bg-stone-100 dark:border-stone-600 dark:hover:bg-stone-800"
        >
          Restore
        </button>
      ) : null}
      {check.isError ? <span className="text-amber-800 dark:text-amber-300">Could not check: {check.error.message}</span> : null}
      {last ? (
        <span data-testid="liveness-result" className={last.result === 'closed' ? 'text-amber-800 dark:text-amber-300' : 'text-stone-600 dark:text-stone-300'}>
          {LIVENESS_TEXT[last.result]}
          {last.detail ? <span className="text-xs text-stone-500"> ({last.detail})</span> : null}
          <span className="block text-xs text-stone-500">checked {timeAgo(last.checked_at)}</span>
        </span>
      ) : (
        <span className="text-xs text-stone-500">Not checked yet. Nothing is requested until you click.</span>
      )}
    </section>
  )
}

export function JobDetailPanel({ jobId, onClose }: Props) {
  const { data: job, isPending, isError, error, refetch } = useJob(jobId)
  const headingRef = useRef<HTMLHeadingElement>(null)

  useEffect(() => {
    headingRef.current?.focus()
  }, [jobId, job?.id])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const hasDescription = Boolean(job?.description_text && job.description_text.trim())

  return (
    <aside
      aria-label="Job details"
      className="sheet-in fixed inset-y-0 right-0 z-40 flex w-full max-w-[620px] flex-col border-l border-stone-200 bg-white shadow-2xl dark:border-stone-800 dark:bg-stone-900"
    >
      <div className="flex items-center justify-between border-b border-stone-200 px-4 py-2 dark:border-stone-800">
        <span className="text-xs font-medium uppercase tracking-wide text-stone-500">Job details</span>
        <button type="button" onClick={onClose} className="rounded px-2 py-1 text-sm hover:bg-stone-100 dark:hover:bg-stone-800" aria-label="Close job details">
          Close <kbd className="ml-1 rounded border border-stone-300 px-1 text-[10px] dark:border-stone-600">Esc</kbd>
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4">
        {isPending ? <Spinner label="Loading job" /> : null}
        {isError ? <ErrorState message={error.message} onRetry={() => void refetch()} /> : null}
        {job ? (
          <article className="space-y-5">
            <header>
              <h2 ref={headingRef} tabIndex={-1} className="text-xl font-semibold leading-snug outline-none">
                {job.title}
              </h2>
              <p className="mt-1 text-stone-600 dark:text-stone-300">
                {job.company_name || 'Unknown company'}
                {job.location ? <span className="text-stone-400"> · {job.location}</span> : null}
              </p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                <LocationBadge fit={job.location_fit} reason={job.location_reason} matched={job.location_matched} />
                <SeniorityBadge level={job.seniority_level} evidence={job.seniority_evidence} />
                <RemoteChips job={job} />
                {job.work_auth_required ? <Chip tone="warn">Work authorization required</Chip> : null}
                {job.experience_level !== 'unknown' ? <Chip>{LEVEL_LABELS[job.experience_level]}</Chip> : null}
                <Chip>{TYPE_LABELS[job.employment_type]}</Chip>
                {job.status === 'closed' ? <Chip tone="warn">Closed {job.closed_at ? timeAgo(job.closed_at) : ''}</Chip> : null}
                <FreshnessChip band={job.freshness} ageDays={job.age_days} autoHidden={job.auto_hidden} />
                {job.graduation_year_hint ? <Chip>Class of {job.graduation_year_hint}</Chip> : null}
                {job.sponsorship_note ? <Chip>{job.sponsorship_note}</Chip> : null}
                {formatSalary(job.salary_min, job.salary_max, job.salary_currency) ? (
                  <Chip>{formatSalary(job.salary_min, job.salary_max, job.salary_currency)}</Chip>
                ) : null}
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5" aria-label="Eligibility and history">
                {job.likely_ineligible ? <Chip tone="warn">{job.likely_ineligible}</Chip> : null}
                {job.applied_company_warning ? <Chip tone="warn">{job.applied_company_warning}</Chip> : null}
                {(job.learned_rules ?? []).map((rule) => (
                  <span key={rule.id ?? rule.phrase} title={rule.tooltip ?? ''}>
                    <Chip>learned rule: {rule.phrase}</Chip>
                  </span>
                ))}
              </div>
              {job.eligibility_screens && job.eligibility_screens.length > 0 ? (
                <p className="mt-2 text-xs text-stone-600 dark:text-stone-300">
                  Possible eligibility screens in the posting:{' '}
                  {job.eligibility_screens
                    .map((screen) => (screen.risk === 'possible_knockout' ? `${screen.label} (above your value)` : screen.label))
                    .join(', ')}
                </p>
              ) : null}
              {job.signals && job.signals.length > 0 ? (
                <ul aria-label="Posting signals" className="mt-2 flex flex-wrap gap-1.5">
                  {job.signals.map((signal) => (
                    <li key={signal.id}>
                      <Chip tone={signal.level === 'caution' ? 'warn' : 'neutral'}>{signal.text}</Chip>
                    </li>
                  ))}
                </ul>
              ) : null}
            </header>

            <div className="flex flex-wrap items-center gap-2">
              <DecisionControls job={job} />
              {isSafeHttpUrl(job.apply_url) ? (
                <a
                  href={job.apply_url}
                  onClick={() => {
                    void fetch(`/api/jobs/${job.id}/opened`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).catch(() => undefined)
                  }}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="rounded-md border border-stone-300 px-3 py-1.5 text-sm font-medium hover:bg-stone-100 dark:border-stone-600 dark:hover:bg-stone-800"
                >
                  Open apply page ↗
                </a>
              ) : (
                <span className="text-sm text-stone-500">No apply link available</span>
              )}
            </div>

            <OpenCheck job={job} />

            <WhyThisMatch job={job} />

            <ResumeBox job={job} />

            <WritingSection job={job} />

            <section aria-label="Description">
              <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-stone-500">Description</h3>
              {hasDescription ? (
                // Plain text node, never HTML: job text is untrusted.
                <div data-testid="job-description" className="whitespace-pre-wrap break-words text-[15px] leading-relaxed">
                  {job.description_text}
                </div>
              ) : (
                <div className="rounded-lg border border-dashed border-stone-300 px-4 py-6 text-sm text-stone-600 dark:border-stone-700 dark:text-stone-400">
                  <p className="font-medium text-stone-800 dark:text-stone-200">No description yet</p>
                  <p className="mt-1">
                    {job.needs_description
                      ? 'This listing only gave us a link (job-alert email, aggregator or curated list). Open the apply page to read it; a later ingest may fill it in when the employer’s board is polled.'
                      : 'The source did not include a description.'}
                  </p>
                </div>
              )}
            </section>

            <section aria-label="Sources">
              <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-stone-500">Seen on</h3>
              <ul className="space-y-2">
                {job.sightings.map((sighting) => (
                  <li key={`${sighting.source}:${sighting.source_job_id}`} className="rounded-lg border border-stone-200 px-3 py-2 text-sm dark:border-stone-800">
                    <div className="flex flex-wrap items-center gap-x-2">
                      <span className="font-medium">{sourceLabel(sighting.source)}</span>
                      <span className="text-xs text-stone-400">first seen {formatDate(sighting.first_seen_at)}</span>
                    </div>
                    {sighting.attribution_text ? (
                      <p className="mt-0.5 text-xs text-stone-500 dark:text-stone-400">{sighting.attribution_text}</p>
                    ) : null}
                    {isSafeHttpUrl(sighting.url) ? (
                      <a href={sighting.url} target="_blank" rel="noopener noreferrer" className="mt-1 inline-block break-all text-xs text-brand-700 underline dark:text-brand-100">
                        {sighting.url}
                      </a>
                    ) : null}
                  </li>
                ))}
              </ul>
            </section>

            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-stone-500">
              <dt>Posted</dt>
              <dd>{job.posted_at ? formatDate(job.posted_at) : 'unknown'}</dd>
              <dt>First seen</dt>
              <dd>{formatDate(job.first_seen_at)}</dd>
              <dt>Last seen</dt>
              <dd>{formatDate(job.last_seen_at)}</dd>
              {job.company ? (
                <>
                  <dt>Board</dt>
                  <dd>
                    {job.company.ats_type} / {job.company.slug}
                  </dd>
                </>
              ) : null}
            </dl>
          </article>
        ) : null}
      </div>
    </aside>
  )
}
