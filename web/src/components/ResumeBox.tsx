import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useResumes, useTailorEstimate, useTailorJob } from '../api/hooks'
import type { JobDetail, TailorEstimate } from '../api/types'
import { formatDate } from '../lib/format'
import { useToast } from './Toast'
import { StrengthChip } from './TailoringStrength'
import { Chip, Spinner } from './ui'

export function EstimateLine({ estimate }: { estimate: TailorEstimate | undefined }) {
  if (!estimate) return <span className="text-xs text-stone-500">Checking the estimate…</span>
  return (
    <span className="text-xs text-stone-600 dark:text-stone-300" data-testid="tailor-estimate">
      {estimate.blocked
        ? `${estimate.message}. It will use the free deterministic path (no model).`
        : estimate.will_use_model
          ? `Estimated ≈ $${estimate.estimated_usd.toFixed(3)} (${estimate.model}).`
          : 'Deterministic path (no model, $0).'}{' '}
      Interactive calls today: {estimate.interactive_calls_today}/{estimate.interactive_call_cap}.
    </span>
  )
}

/** Explicit confirm step for "Re-tailor": shows the cost and the interactive counter, then one POST on confirm. */
export function RetailorConfirm({ job, base, onDone }: { job: { id: number }; base?: 'ai' | 'fullstack' | 'backend'; onDone: () => void }) {
  const estimate = useTailorEstimate(job.id, true)
  const tailor = useTailorJob()
  const { notify } = useToast()
  const run = () =>
    tailor.mutate(
      { jobId: job.id, base, force: true },
      {
        onSuccess: (result) => {
          notify(
            result.status === 'failed'
              ? `Re-tailoring failed: ${result.message ?? 'see the Resumes page'}`
              : `Created a new version (${result.mode === 'advised' ? 'Gemini advised' : 'deterministic'}). Old versions are kept.`,
            result.status === 'failed' ? 'error' : 'info',
          )
          onDone()
        },
        onError: (error) => notify(`Could not tailor: ${error.message}`, 'error'),
      },
    )
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 rounded border border-stone-300 px-2 py-1.5 dark:border-stone-700" role="group" aria-label="Confirm re-tailor">
      <EstimateLine estimate={estimate.data} />
      <button
        type="button"
        onClick={run}
        disabled={tailor.isPending || !estimate.data}
        className="rounded-md bg-brand-700 px-2.5 py-1 text-xs font-medium text-white hover:bg-brand-800 disabled:opacity-60"
      >
        {tailor.isPending ? 'Tailoring…' : 'Confirm re-tailor (new version)'}
      </button>
      <button type="button" onClick={onDone} className="text-xs underline">
        Cancel
      </button>
    </div>
  )
}

/**
 * The job panel's resume box. Opening a job never tailors and never downloads: this box only reads the list of
 * versions. Tailoring is the button below (a POST), downloading is the Download link (a separate endpoint).
 */
export function ResumeBox({ job }: { job: JobDetail }) {
  const versions = useResumes({ jobId: job.id, includeSuperseded: true })
  const tailor = useTailorJob()
  const { notify } = useToast()
  const [base, setBase] = useState<'' | 'ai' | 'fullstack'>('')
  const [confirming, setConfirming] = useState(false)
  const items = versions.data?.items ?? []
  const latest = items[0]
  const estimate = useTailorEstimate(job.id, !versions.isPending && !latest)
  const recommended = job.best_profile ?? 'ai'

  const first = () =>
    tailor.mutate(
      { jobId: job.id, base: base || undefined },
      {
        onSuccess: (result) =>
          notify(result.status === 'failed' ? `Tailoring failed: ${result.message ?? ''}` : 'Resume created. Open “View” to see it.',
            result.status === 'failed' ? 'error' : 'info'),
        onError: (error) => notify(`Could not tailor this resume: ${error.message}`, 'error'),
      },
    )

  return (
    <section aria-label="Resume" className="rounded-lg border border-stone-200 px-4 py-3 dark:border-stone-800">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-stone-500">Resume</h3>
      {versions.isPending ? <Spinner label="Checking versions" /> : null}
      {latest ? (
        <div className="mt-2 space-y-1.5 text-sm">
          <p>
            <strong>{latest.code}</strong> v{latest.version}{' '}
            <span className="text-stone-500">(created {formatDate(latest.created_at)})</span>{' '}
            <Chip>{latest.lifecycle}</Chip>
            <StrengthChip strength={latest.tailoring_strength} />
            {latest.stale_profile ? <Chip tone="warn">your facts changed since</Chip> : null}
          </p>
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Link className="text-brand-700 underline dark:text-brand-100" to={`/resumes?resume=${latest.id}`}>
              View
            </Link>
            {latest.has_pdf ? (
              <a className="text-brand-700 underline dark:text-brand-100" href={`/api/resumes/${latest.id}/download`}>
                Download
              </a>
            ) : (
              <span className="text-stone-400">No PDF file</span>
            )}
            <button type="button" className="text-brand-700 underline dark:text-brand-100" onClick={() => setConfirming((v) => !v)} aria-expanded={confirming}>
              Re-tailor
            </button>
            <Link className="text-brand-700 underline dark:text-brand-100" to={`/resumes?job=${job.id}&all=1`}>
              All versions ({items.length})
            </Link>
          </p>
          <p className="text-xs text-stone-500">
            {latest.advice_source === 'gemini'
              ? `Gemini advised (${latest.model}, $${latest.cost_usd.toFixed(3)})`
              : 'Deterministic path (no model)'}
            . Resume text is never written by the model.
          </p>
          {confirming ? <RetailorConfirm job={job} base={latest.base} onDone={() => setConfirming(false)} /> : null}
        </div>
      ) : versions.isPending ? null : (
        <div className="mt-2 space-y-1.5 text-sm">
          <p className="text-xs text-stone-500">No resume for this job yet. Nothing is created until you click Tailor.</p>
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1">
              <span className="text-stone-500">Base</span>
              <select
                value={base}
                onChange={(event) => setBase(event.target.value as '' | 'ai' | 'fullstack')}
                className="rounded border border-stone-300 bg-white px-1.5 py-1 dark:border-stone-700 dark:bg-stone-900"
              >
                <option value="">Recommended ({recommended === 'ai' ? 'AI / ML' : 'Full-stack'})</option>
                <option value="ai">AI / ML</option>
                <option value="fullstack">Full-stack</option>
              </select>
            </label>
            <button
              type="button"
              onClick={first}
              disabled={tailor.isPending}
              className="rounded-md bg-brand-700 px-3 py-1.5 font-medium text-white hover:bg-brand-800 disabled:opacity-60"
            >
              {tailor.isPending ? 'Tailoring…' : 'Tailor'}
            </button>
            <EstimateLine estimate={estimate.data} />
          </div>
        </div>
      )}
    </section>
  )
}
