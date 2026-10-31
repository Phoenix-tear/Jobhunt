import { Link, NavLink } from 'react-router-dom'
import { ScheduleSection } from '../components/ScheduleSection'
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { requestJson } from '../api/client'
import { useHealth, useRunTriageNow, useTailoringStatus } from '../api/hooks'
import { CompanyRulesSection } from '../components/CompanyRulesSection'
import { FreshnessSection } from '../components/FreshnessSection'
import { LearningSection } from '../components/LearningSection'
import { SkillGapsSection } from '../components/TailoringStrength'
import type { Health, SourceHealth, ReadinessChecklist, DoctorResult } from '../api/types'
import { STAGE_LABELS, STAGES } from '../api/types'
import { Chip, ErrorState, Spinner } from '../components/ui'
import { formatDate, sourceLabel, timeAgo } from '../lib/format'

const WARNING_HINTS: Record<string, string> = {
  user_agent_contact_missing: 'Set user_agent_contact in config.yaml (a URL or email).',
  gmail_token_missing: 'Follow SETUP.md, then run: .\.venv\Scripts\python.exe -m jobhunt gmail-auth',
  gmail_scope_unexpected: 'Delete .secrets/token.json and run: .\.venv\Scripts\python.exe -m jobhunt gmail-auth',
  source_stale: 'Run: .\.venv\Scripts\python.exe -m jobhunt ingest (or check the scheduled task).',
  source_never_run: 'Run: .\.venv\Scripts\python.exe -m jobhunt ingest',
  source_errors: 'Open the source row below for the first error.',
}

function Tile({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="rounded-lg border border-stone-200 bg-white px-4 py-3 dark:border-stone-800 dark:bg-stone-900">
      <p className="text-xs uppercase tracking-wide text-stone-500">{label}</p>
      <p className="mt-0.5 text-2xl font-semibold tabular-nums">{value}</p>
      {hint ? <p className="text-xs text-stone-400">{hint}</p> : null}
    </div>
  )
}

function StatusDot({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden className={`size-2 rounded-full ${ok ? 'bg-emerald-500' : 'bg-amber-500'}`} />
      {label}
    </span>
  )
}

function sourceStatus(source: SourceHealth): { tone: 'brand' | 'warn' | 'neutral'; label: string } {
  if (!source.enabled) return { tone: 'neutral', label: 'Disabled' }
  if (source.never_run) return { tone: 'neutral', label: 'Never run' }
  if (source.stale) return { tone: 'warn', label: 'Stale' }
  if (source.last_success_at === null) return { tone: 'warn', label: 'No success yet' }
  return { tone: 'brand', label: 'OK' }
}

function SourceRow({ source }: { source: SourceHealth }) {
  const status = sourceStatus(source)
  const run = source.last_run
  return (
    <tr className="border-t border-stone-200 align-top dark:border-stone-800" data-source={source.source}>
      <th scope="row" className="py-2 pr-3 text-left font-medium">
        {sourceLabel(source.source)}
        <span className="ml-2 text-xs font-normal text-stone-400">{source.kind}</span>
      </th>
      <td className="py-2 pr-3">
        <Chip tone={status.tone}>{status.label}</Chip>
      </td>
      <td className="py-2 pr-3 text-sm text-stone-600 dark:text-stone-300">
        {source.last_success_at ? (
          <>
            {timeAgo(source.last_success_at)}
            <span className="block text-xs text-stone-400">{formatDate(source.last_success_at)}</span>
          </>
        ) : (
          <span className="text-stone-400">never</span>
        )}
      </td>
      <td className="py-2 pr-3 text-sm tabular-nums text-stone-600 dark:text-stone-300">
        {run ? (
          <>
            {run.jobs_seen.toLocaleString()} seen · {run.jobs_new.toLocaleString()} new · {run.jobs_closed.toLocaleString()} closed
            {run.companies_checked ? <span className="block text-xs text-stone-400">{run.companies_checked.toLocaleString()} boards checked</span> : null}
          </>
        ) : (
          <span className="text-stone-400">—</span>
        )}
      </td>
      <td className="py-2 text-sm">
        {run && run.error_count > 0 ? (
          <span className="text-rose-700 dark:text-rose-300">
            {run.error_count} error{run.error_count === 1 ? '' : 's'}
            {run.first_error ? <span className="block max-w-md truncate text-xs" title={run.first_error}>{run.first_error}</span> : null}
          </span>
        ) : (
          <span className="text-stone-400">none</span>
        )}
      </td>
    </tr>
  )
}

function UsageBar({ label, used, cap, format }: { label: string; used: number; cap: number; format: (value: number) => string }) {
  const share = cap > 0 ? Math.min(1, used / cap) : 0
  return (
    <li>
      <div className="flex justify-between text-sm">
        <span>{label}</span>
        <span className="tabular-nums text-stone-500">
          {format(used)} of {format(cap)}
        </span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded bg-stone-200 dark:bg-stone-800" role="presentation">
        <div className={`h-full ${share >= 0.9 ? 'bg-amber-500' : 'bg-brand-600'}`} style={{ width: `${share * 100}%` }} />
      </div>
    </li>
  )
}

function MatchingSection({ matching }: { matching: NonNullable<Health['matching']> }) {
  const { usage } = matching
  return (
    <section aria-label="Matching" className="rounded-lg border border-stone-200 bg-white px-4 py-3 text-sm dark:border-stone-800 dark:bg-stone-900">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-stone-500">Matching &amp; AI budget</h2>
      <ul className="grid gap-3 md:grid-cols-2">
        <UsageBar label="AI spend this month" used={usage.usd_month} cap={usage.monthly_usd_cap} format={(value) => `$${value.toFixed(2)}`} />
        <UsageBar label="Pipeline AI calls today (score, classify)" used={usage.calls_today} cap={usage.daily_call_cap} format={(value) => value.toLocaleString()} />
        <UsageBar label="Interactive AI calls today (tailor, letters, answers; resets 05:30 IST)" used={usage.interactive_calls_today ?? 0} cap={usage.interactive_call_cap ?? 40} format={(value) => value.toLocaleString()} />
        <UsageBar label="Embeddings today" used={usage.embedded_texts_today} cap={usage.daily_embedding_cap} format={(value) => value.toLocaleString()} />
        <UsageBar
          label={`Embedding coverage (${matching.embedded.toLocaleString()} of ${matching.embeddable.toLocaleString()} jobs)`}
          used={Math.round(matching.embedding_coverage * 100)}
          cap={100}
          format={(value) => `${value}%`}
        />
      </ul>
      {matching.budget_forecast ? (
        <div className="mt-3 rounded border border-stone-200 bg-stone-50 p-2.5 text-xs text-stone-700 dark:border-stone-800 dark:bg-stone-800/50 dark:text-stone-300" data-testid="budget-forecast">
          <div className="flex flex-wrap items-center justify-between gap-1">
            <div>
              <span className="font-semibold text-stone-900 dark:text-stone-100">Budget forecast: </span>
              <span>Month-to-date <strong>${matching.budget_forecast.month_to_date_usd.toFixed(2)}</strong></span>
              <span> &bull; 7-day avg <strong>${matching.budget_forecast.avg_daily_usd_7d.toFixed(2)}</strong>/day</span>
              <span> &bull; Projected month total <strong>${matching.budget_forecast.projected_month_usd.toFixed(2)}</strong> (cap: ${matching.budget_forecast.cap_usd.toFixed(2)})</span>
              {matching.budget_forecast.cap_hit_date ? (
                <span> &bull; Cap hit day: <strong>{matching.budget_forecast.cap_hit_date}</strong></span>
              ) : null}
            </div>
            {matching.budget_forecast.warning ? (
              <span className="rounded bg-amber-100 px-2 py-0.5 font-semibold text-amber-800 dark:bg-amber-900/50 dark:text-amber-200" data-testid="budget-forecast-warning">
                Warning: Projected spend exceeds monthly cap!
              </span>
            ) : null}
          </div>
        </div>
      ) : null}
      {matching.projection ? (
        <p className="mt-3 text-sm" data-testid="projection">
          Projected Gemini cost: <strong>${matching.projection.usd_per_month.toFixed(2)}</strong> / month at{' '}
          {matching.projection.calls_per_day} jobs a day ({matching.projection.model}, ~
          {matching.projection.avg_input_tokens.toLocaleString()} input tokens per job)
          {matching.projection.price_change_on ? (
            <>
              {' '}
              and <strong>${matching.projection.usd_per_month_after_price_change.toFixed(2)}</strong> after the price change on{' '}
              {matching.projection.price_change_on}
            </>
          ) : null}
          .
        </p>
      ) : null}
      {matching.local_stages.length > 0 ? (
        <ul className="mt-2 grid gap-1 text-xs text-stone-500 md:grid-cols-3" aria-label="Local stages">
          {matching.local_stages.map((stage) => (
            <li key={stage.stage}>
              <strong className="text-stone-700 dark:text-stone-300">{stage.stage}</strong>:{' '}
              {stage.last_run_at
                ? `${stage.status ?? 'ok'}, ${timeAgo(stage.last_run_at)}, ${stage.seconds ?? 0}s, ${stage.items ?? 0} items${stage.model ? ` (${stage.model})` : ''}`
                : 'has not run'}
            </li>
          ))}
        </ul>
      ) : null}
      <p className="mt-3 text-xs text-stone-500">
        {matching.scored_jobs.toLocaleString()} jobs scored by the AI · spent today ${usage.usd_today.toFixed(2)} · last relevance run{' '}
        {matching.last_relevance_run_at ? timeAgo(matching.last_relevance_run_at) : 'never'} · last AI call{' '}
        {matching.last_llm_call_at ? timeAgo(matching.last_llm_call_at) : 'never'}
        {matching.profiles.length === 0 ? ' · no resume profiles built yet (run profile-build)' : ''}
      </p>
    </section>
  )
}

function TriageSection({ triage }: { triage: NonNullable<Health['triage']> }) {
  const percent = (value: number | null | undefined) => (value === null || value === undefined ? 'n/a' : `${(value * 100).toFixed(1)}%`)
  const tierMix = triage.tier_mix_7d ?? {}
  const heads = triage.heads_status ?? {}
  const runTriage = useRunTriageNow()

  return (
    <section aria-label="LLM triage" className="rounded-lg border border-stone-200 bg-white px-4 py-3 text-sm dark:border-stone-800 dark:bg-stone-900">
      <div className="flex items-center justify-between mb-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-stone-500">LLM Triage</h2>
          {triage.coverage_visible_relevant_pct != null ? (
            <span className="rounded bg-sky-50 px-2 py-0.5 text-xs font-medium text-sky-700 dark:bg-sky-950 dark:text-sky-300">
              Coverage: {triage.coverage_visible_relevant_judged ?? 0} of {triage.coverage_visible_relevant_total ?? 0} visible Relevant ({triage.coverage_visible_relevant_pct.toFixed(1)}%) · Gemini: {triage.coverage_gemini ?? 0} · Local only: {triage.coverage_local_only ?? 0} · None: {triage.coverage_none ?? 0}
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          {triage.gemini_calls_saved_7d != null && triage.gemini_calls_saved_7d > 0 ? (
            <span className="rounded bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
              {triage.gemini_calls_saved_7d} Gemini calls saved (7d)
            </span>
          ) : null}
          <button
            type="button"
            disabled={runTriage.isPending}
            onClick={() => runTriage.mutate()}
            className="rounded bg-stone-800 px-2.5 py-1 text-xs font-medium text-white hover:bg-stone-700 disabled:opacity-50 dark:bg-stone-200 dark:text-stone-900 dark:hover:bg-stone-300"
          >
            {runTriage.isPending ? 'Judging...' : 'Run triage now'}
          </button>
        </div>
      </div>
      <ul className="grid gap-1 md:grid-cols-2">
        <li>
          Verdicts: <strong>{triage.verdicts.toLocaleString()}</strong> ({triage.model}, rubric {triage.rubric_version}
          {triage.enabled ? '' : ', switched off'})
        </li>
        <li>
          Calls today: <strong>{triage.calls_today}</strong> of {triage.daily_calls_cap}
        </li>
        <li data-testid="triage-hallucinated">
          Evidence not verbatim in posting: <strong>{percent(triage.not_verbatim_rate ?? triage.hallucinated_evidence_rate)}</strong> ({triage.not_verbatim_fields ?? triage.hallucinated_fields} claims)
        </li>
        <li data-testid="triage-irrelevant">
          Irrelevant evidence rate: <strong>{percent(triage.irrelevant_evidence_rate)}</strong> ({triage.irrelevant_fields ?? 0} claims)
        </li>
        <li data-testid="triage-leak">
          Leak rate, last {triage.judged_in_window} judged: <strong>{percent(triage.leak_rate_last_100)}</strong> ({triage.leaked_in_window} still in Relevant)
        </li>
        <li data-testid="triage-demoted">
          Demoted this week: <strong>{triage.demoted_week_llm}</strong> by the LLM · <strong>{triage.demoted_week_classifier}</strong> by the classifier ·{' '}
          <strong>{triage.demoted_week_rules}</strong> by rules
        </li>
        <li data-testid="triage-agreement">
          My agreement: <strong>{percent(triage.agreement)}</strong> ({triage.verdict_wrong_marked} of {triage.verdicts_that_acted} acting verdicts marked wrong)
        </li>
        <li>
          Queue length: <strong>{triage.queue_length ?? 0}</strong> unjudged · Last `jobhunt-triage` run: <strong>{triage.last_jobhunt_triage_run ? timeAgo(triage.last_jobhunt_triage_run) : (triage.last_run_duration_seconds != null ? `${triage.last_run_duration_seconds.toFixed(1)}s` : 'never')}</strong>
        </li>
        <li>
          Disagreement audit progress: <strong>{triage.audit_progress ?? 'imported 0 of 60'}</strong>{' '}
          ·{' '}
          <Link
            to="/audit"
            className="font-medium text-brand-600 underline hover:text-brand-700 dark:text-brand-400"
          >
            Review audit (clicks) →
          </Link>
        </li>
        <li>
          Gemini confirmations: <strong>{triage.gemini_confirmations_used ?? 0}</strong> · Models disagree: <strong>{triage.models_disagree_count ?? 0}</strong>
        </li>
      </ul>

      {triage.acting_rules && triage.acting_rules.length > 0 ? (
        <div className="mt-3 border-t border-stone-100 pt-2 dark:border-stone-800" data-testid="acting-rules-table">
          <p className="text-xs font-semibold uppercase tracking-wide text-stone-500 mb-1.5">Acting Rules Table (Consensus Guard &amp; Wilson 95% Intervals)</p>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-stone-200 dark:border-stone-800 text-stone-400">
                  <th className="pb-1 font-medium">Field</th>
                  <th className="pb-1 font-medium">Target Class</th>
                  <th className="pb-1 font-medium">Mode</th>
                  <th className="pb-1 font-medium">Rule</th>
                  <th className="pb-1 font-medium">N (Human)</th>
                  <th className="pb-1 font-medium">Precision (95% CI)</th>
                  <th className="pb-1 font-medium">Recall</th>
                  <th className="pb-1 font-medium">Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-stone-100 dark:divide-stone-800 text-stone-600 dark:text-stone-300">
                {triage.acting_rules.map((rule) => (
                  <tr key={`${rule.field}-${rule.target_class}`}>
                    <td className="py-1 font-mono">{rule.field}</td>
                    <td className="py-1 font-mono">{rule.target_class}</td>
                    <td className="py-1">
                      <span className={`rounded px-1.5 py-0.2 text-[10px] font-semibold uppercase ${rule.mode === 'gemini_alone' ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200' : 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200'}`}>
                        {rule.mode}
                      </span>
                    </td>
                    <td className="py-1 text-[11px] text-stone-500 font-mono">{rule.rule_desc || 'consensus'}</td>
                    <td className="py-1">{rule.sample_count}</td>
                    <td className="py-1 font-semibold">
                      {percent(rule.precision)}
                      {rule.ci_lower != null && rule.ci_upper != null ? (
                        <span className="block text-[10px] font-normal text-stone-400">
                          [{percent(rule.ci_lower)}, {percent(rule.ci_upper)}]
                        </span>
                      ) : null}
                    </td>
                    <td className="py-1">{percent(rule.recall)}</td>
                    <td className="py-1 text-stone-500 truncate max-w-xs">{rule.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {Object.keys(tierMix).length > 0 ? (
        <div className="mt-3 border-t border-stone-100 pt-2 text-xs text-stone-600 dark:border-stone-800 dark:text-stone-300">
          <span className="font-semibold text-stone-500">Cascade tier mix (7d): </span>
          Rules: <strong>{tierMix.rules ?? 0}</strong> · Classifier: <strong>{tierMix.classifier ?? 0}</strong> · Local LLM: <strong>{tierMix.local_llm ?? 0}</strong> · Gemini: <strong>{tierMix.gemini ?? 0}</strong>
        </div>
      ) : null}

      {Object.keys(heads).length > 0 ? (
        <div className="mt-3 border-t border-stone-100 pt-2 dark:border-stone-800">
          <p className="text-xs font-semibold uppercase tracking-wide text-stone-500 mb-1">Heads status (Background)</p>
          <div className="grid gap-2 sm:grid-cols-3">
            {Object.entries(heads).map(([name, h]) => (
              <div key={name} className="rounded border border-stone-100 bg-stone-50 p-2 text-xs dark:border-stone-800 dark:bg-stone-950">
                <div className="flex items-center justify-between font-medium">
                  <span className="capitalize">{name}</span>
                  <span className={`rounded px-1.5 py-0.2 text-[10px] font-semibold uppercase ${h.mode === 'active' ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200' : 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200'}`}>
                    {h.mode}
                  </span>
                </div>
                <p className="mt-1 text-stone-500">
                  Labels: {h.n_pos} pos / {h.n_neg} neg
                </p>
                {h.precision != null ? (
                  <p className="text-stone-500">
                    Precision: <strong>{percent(h.precision)}</strong> · Coverage: <strong>{percent(h.coverage)}</strong>
                  </p>
                ) : null}
                {h.needs_hint ? (
                  <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-400 font-medium">
                    {h.needs_hint}
                  </p>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {triage.last_trained_at ? (
        <p className="mt-2 text-xs text-stone-400">
          Last model training: {triage.last_trained_at} · Labels: {triage.label_counts?.human ?? 0} human, {triage.label_counts?.verified ?? 0} verified, {triage.label_counts?.weak ?? 0} weak
        </p>
      ) : null}

      {Object.keys(triage.by_level).length > 0 ? (
        <p className="mt-2 text-xs text-stone-500">
          By level: {Object.entries(triage.by_level).map(([level, count]) => `${level.replace('_', ' ')} ${count}`).join(' · ')}
        </p>
      ) : null}
    </section>
  )
}

function TailoringSection() {
  const status = useTailoringStatus()
  if (!status.data) return null
  const data = status.data
  return (
    <section aria-label="Tailoring" className="rounded-lg border border-stone-200 bg-white px-4 py-3 text-sm dark:border-stone-800 dark:bg-stone-900">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-stone-500">Resume tailoring</h2>
      <ul className="grid gap-1 md:grid-cols-2">
        <li>
          <StatusDot ok={Boolean(data.engine)} label={data.engine ? `LaTeX engine: ${data.engine}` : 'No LaTeX engine (the .tex is written, the PDF is not compiled)'} />
        </li>
        <li className="text-xs text-stone-500">
          {data.runs_today} run(s) today · ${data.usd_today.toFixed(4)} today · ${data.usd_month.toFixed(4)} this month
        </li>
        <li className="text-xs text-stone-500">
          Validator rejected {data.validator_rejections} of {data.llm_runs} model plan(s) ({Math.round(data.validator_failure_rate * 100)}%)
        </li>
        <li className="text-xs text-stone-500">{data.failed} failed run(s)</li>
      </ul>
    </section>
  )
}

function ReadinessSection({ readiness }: { readiness?: ReadinessChecklist | null }) {
  if (!readiness) return null
  const items = [
    readiness.key_set,
    readiness.resume_validated,
    readiness.facts_ticked,
    readiness.profile_complete,
    readiness.sources_chosen,
    readiness.first_ingest_done,
    readiness.first_match_done,
  ].filter(Boolean)

  const passedCount = items.filter((i) => i.passed).length

  return (
    <section aria-label="Readiness Checklist" className="rounded-lg border border-stone-200 bg-white p-4 shadow-sm dark:border-stone-800 dark:bg-stone-900">
      <div className="flex items-center justify-between mb-3 border-b border-stone-100 pb-2 dark:border-stone-800">
        <h2 className="text-sm font-semibold tracking-wide uppercase text-stone-600 dark:text-stone-300">
          Setup Readiness Checklist ({passedCount}/{items.length} Ready)
        </h2>
        <span className={`text-xs px-2.5 py-0.5 rounded-full font-medium ${readiness.ready ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300' : 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300'}`}>
          {readiness.ready ? 'All Systems Ready' : 'Setup Incomplete'}
        </span>
      </div>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((item) => (
          <div key={item.id} className="flex items-start gap-2.5 p-2 rounded-md border border-stone-100 dark:border-stone-800/60 bg-stone-50/50 dark:bg-stone-950/40">
            <span className={`mt-0.5 size-4 rounded-full flex items-center justify-center text-[10px] font-bold shrink-0 ${item.passed ? 'bg-emerald-500 text-white' : 'bg-amber-500 text-stone-950'}`}>
              {item.passed ? '✓' : '!'}
            </span>
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between gap-1">
                <span className="text-xs font-medium text-stone-900 dark:text-stone-100 truncate">{item.label}</span>
                <NavLink to={item.link} className="text-[11px] text-brand-600 hover:underline shrink-0 dark:text-brand-400 font-medium">
                  Configure →
                </NavLink>
              </div>
              <p className="text-[11px] text-stone-500 dark:text-stone-400 mt-0.5 line-clamp-2">{item.guidance}</p>
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

function SystemCheckSection() {
  const [data, setData] = useState<DoctorResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const runCheck = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await requestJson<DoctorResult>('/api/health/doctor')
      setData(res)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'System check failed')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void runCheck()
  }, [runCheck])

  return (
    <section aria-label="System check" className="rounded-lg border border-stone-200 bg-white px-4 py-3 text-sm dark:border-stone-800 dark:bg-stone-900">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-stone-500">System check (Jobhunt Doctor)</h2>
        <button
          type="button"
          onClick={() => void runCheck()}
          disabled={loading}
          className="rounded border border-stone-300 px-2 py-1 text-xs font-medium hover:bg-stone-50 disabled:opacity-50 dark:border-stone-700 dark:hover:bg-stone-800"
        >
          {loading ? 'Checking...' : 'Re-run check'}
        </button>
      </div>

      {loading && !data ? <p className="text-xs text-stone-400">Running diagnostic checks...</p> : null}
      {error ? <p className="text-xs text-rose-500">Error: {error}</p> : null}

      {data ? (
        <div className="space-y-2">
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {data.checks.map((chk) => {
              const tone = chk.status === 'pass' ? 'emerald' : chk.status === 'warn' ? 'amber' : 'rose'
              return (
                <div key={chk.id} className="rounded border border-stone-100 p-2.5 dark:border-stone-800">
                  <div className="flex items-center justify-between gap-1">
                    <span className="font-medium text-xs text-stone-700 dark:text-stone-300">{chk.name}</span>
                    <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
                      tone === 'emerald' ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300' :
                      tone === 'amber' ? 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300' :
                      'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300'
                    }`}>
                      {chk.status}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-stone-500 dark:text-stone-400 truncate" title={chk.message}>{chk.message}</p>
                  {chk.hint ? <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-400 font-mono">{chk.hint}</p> : null}
                </div>
              )
            })}
          </div>
        </div>
      ) : null}
    </section>
  )
}

function HealthBody({ health }: { health: Health }) {
  const enabledSources = health.sources.filter((source) => source.enabled || !source.never_run)
  const trackedTotal = Object.values(health.applications_by_stage).reduce((sum, count) => sum + count, 0)

  return (
    <div className="space-y-6">
      <ReadinessSection readiness={health.readiness} />

      <SystemCheckSection />

      {health.warnings.length === 0 ? (
        <p role="status" className="rounded-lg border border-emerald-300 bg-emerald-50 px-4 py-3 text-sm text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-100">
          Everything looks healthy: sources are fresh and the configuration is complete.
        </p>
      ) : (
        <ul aria-label="Warnings" className="space-y-2">
          {health.warnings.map((warning, index) => (
            <li key={`${warning.code}-${index}`} role="alert" className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">
              <p className="font-medium">{warning.message}</p>
              {WARNING_HINTS[warning.code] ? <p className="mt-0.5 text-xs opacity-80">{WARNING_HINTS[warning.code]}</p> : null}
            </li>
          ))}
        </ul>
      )}

      <section aria-label="Totals" className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <Tile label="Open jobs" value={health.jobs_open.toLocaleString()} />
        <Tile label="Closed jobs" value={health.jobs_closed.toLocaleString()} />
        <Tile label="Boards active" value={health.companies_active.toLocaleString()} />
        <Tile label="Boards unverified" value={health.companies_unverified.toLocaleString()} hint="probed nightly" />
        <Tile label="Boards inactive" value={health.companies_inactive.toLocaleString()} />
        <Tile
          label="Tracked"
          value={trackedTotal}
          hint={STAGES.filter((stage) => health.applications_by_stage[stage]).map((stage) => `${health.applications_by_stage[stage]} ${STAGE_LABELS[stage].toLowerCase()}`).join(' · ') || undefined}
        />
      </section>

      {health.last_morning_check ? (
        <section aria-label="Morning check" className="rounded-lg border border-stone-200 bg-white px-4 py-3 text-sm dark:border-stone-800 dark:bg-stone-900" data-testid="morning-check-card">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-stone-500">Last morning check</h2>
          <pre className="max-h-64 overflow-auto rounded bg-stone-50 p-3 font-mono text-xs text-stone-800 dark:bg-stone-950 dark:text-stone-200 whitespace-pre-wrap">
            {health.last_morning_check}
          </pre>
        </section>
      ) : null}

      {health.schedule ? <ScheduleSection schedule={health.schedule} /> : null}
      {health.matching ? <MatchingSection matching={health.matching} /> : null}
      {health.freshness ? <FreshnessSection data={health.freshness} /> : null}
      {health.triage ? <TriageSection triage={health.triage} /> : null}
      <TailoringSection />
      <SkillGapsSection />
      <LearningSection />
      <CompanyRulesSection />

      <section aria-label="Sources">
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-stone-500">Sources</h2>
        <div className="overflow-x-auto rounded-lg border border-stone-200 bg-white px-4 dark:border-stone-800 dark:bg-stone-900">
          <table className="w-full min-w-[720px] text-left text-sm">
            <thead>
              <tr className="text-xs uppercase tracking-wide text-stone-500">
                <th className="py-2 pr-3 font-medium">Source</th>
                <th className="py-2 pr-3 font-medium">Status</th>
                <th className="py-2 pr-3 font-medium">Last success</th>
                <th className="py-2 pr-3 font-medium">Last run</th>
                <th className="py-2 font-medium">Errors</th>
              </tr>
            </thead>
            <tbody>
              {enabledSources.map((source) => (
                <SourceRow key={source.source} source={source} />
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <div className="grid gap-4 md:grid-cols-2">
        <section aria-label="Gmail" className="rounded-lg border border-stone-200 bg-white px-4 py-3 text-sm dark:border-stone-800 dark:bg-stone-900">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-stone-500">Gmail alerts</h2>
          <ul className="space-y-1">
            <li>
              <StatusDot ok={health.gmail.credentials_file_exists} label={health.gmail.credentials_file_exists ? 'credentials.json present' : 'credentials.json missing'} />
            </li>
            <li>
              <StatusDot ok={health.gmail.token_file_exists} label={health.gmail.token_file_exists ? 'token.json present' : 'token.json missing (run gmail-auth)'} />
            </li>
            <li>
              <StatusDot
                ok={health.gmail.read_only_scope}
                label={health.gmail.read_only_scope ? 'read-only scope (gmail.readonly)' : health.gmail.token_file_exists ? 'unexpected scopes on token' : 'no token to check'}
              />
            </li>
            {health.gmail.account_configured ? <li className="text-xs text-stone-500">Mailbox: {health.gmail.account_configured}</li> : null}
          </ul>
          <p className="mt-2 text-xs text-stone-400">Checked from local files only; Google is not contacted.</p>
        </section>

        <section aria-label="Configuration" className="rounded-lg border border-stone-200 bg-white px-4 py-3 text-sm dark:border-stone-800 dark:bg-stone-900">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-stone-500">Configuration</h2>
          <ul className="space-y-1">
            <li>
              <StatusDot ok={health.user_agent_contact_set} label={health.user_agent_contact_set ? 'user_agent_contact is set' : 'user_agent_contact is empty'} />
            </li>
            <li className="text-xs text-stone-500">Database schema version {health.schema_version}</li>
            <li className="text-xs text-stone-500">Report generated {timeAgo(health.generated_at)}</li>
          </ul>
        </section>
      </div>
    </div>
  )
}

export function HealthPage() {
  const health = useHealth()
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Health</h1>
      {health.isPending ? <Spinner label="Checking" /> : null}
      {health.isError ? <ErrorState message={health.error.message} onRetry={() => void health.refetch()} /> : null}
      {health.data ? <HealthBody health={health.data} /> : null}
    </div>
  )
}
