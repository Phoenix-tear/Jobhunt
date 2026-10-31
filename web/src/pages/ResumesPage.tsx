import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { requestJson } from '../api/client'
import { useApproveResume, useArchiveResume, useMarkResumeSent, useResume, useResumes } from '../api/hooks'
import type { ResumeDetail, ResumeItem } from '../api/types'
import { RetailorConfirm } from '../components/ResumeBox'
import { StrengthChip, StrengthDetail } from '../components/TailoringStrength'
import { useToast } from '../components/Toast'
import { Chip, EmptyState, ErrorState, Spinner } from '../components/ui'
import { formatDate } from '../lib/format'

function baseLabel(base: string): string {
  return base === 'ai' ? 'AI / ML' : base === 'backend' ? 'Backend' : 'Full-stack'
}

const inputClass = 'rounded border border-stone-300 bg-white px-2 py-1 text-sm dark:border-stone-700 dark:bg-stone-900'
const buttonClass =
  'rounded-md border border-stone-300 px-2.5 py-1 text-xs font-medium hover:bg-stone-100 disabled:opacity-50 dark:border-stone-600 dark:hover:bg-stone-800'

const LIFECYCLE_TONE: Record<string, 'neutral' | 'brand' | 'warn'> = {
  draft: 'neutral',
  approved: 'brand',
  sent: 'brand',
  superseded: 'neutral',
}

function ValidatorBadge({ status }: { status: ResumeItem['validator_status'] }) {
  if (!status) return <Chip>not recorded</Chip>
  return <Chip tone={status === 'ok' ? 'brand' : 'warn'}>{status === 'ok' ? 'validated ✓' : 'validator failed'}</Chip>
}

function Detail({ id }: { id: number }) {
  const detail = useResume(id)
  const approve = useApproveResume()
  const sent = useMarkResumeSent()
  const archive = useArchiveResume()
  const { notify } = useToast()
  const [retailoring, setRetailoring] = useState(false)

  if (detail.isPending) return <Spinner label="Loading the resume" />
  if (detail.isError) return <ErrorState message={detail.error.message} onRetry={() => void detail.refetch()} />
  const item: ResumeDetail = detail.data
  const copyPath = async () => {
    try {
      await navigator.clipboard.writeText(item.pdf_path ?? '')
      notify('Path copied.', 'info')
    } catch {
      notify('Could not copy: select the path and copy it manually.', 'error')
    }
  }

  return (
    <section aria-label="Resume details" className="space-y-3 rounded-lg border border-stone-200 bg-white p-4 dark:border-stone-800 dark:bg-stone-900">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold">
          {item.code} v{item.version} · {item.job_title}
          <span className="font-normal text-stone-500"> · {item.company_name}</span>
        </h2>
        <Chip tone={LIFECYCLE_TONE[item.lifecycle]}>{item.lifecycle}</Chip>
      </div>
      <p className="text-xs text-stone-600 dark:text-stone-300">{item.how_made}</p>
      {item.stale_profile ? (
        <p className="text-xs text-amber-800 dark:text-amber-300">Your ticked facts changed after this version was made. Re-tailor to use them.</p>
      ) : null}
      {item.file_note ? <p className="text-xs text-amber-800 dark:text-amber-300">Note: {item.file_note}. The preview shows the newest file, not this run.</p> : null}

      <div className="flex flex-wrap items-center gap-2">
        {item.has_pdf ? (
          <a className={buttonClass} href={`/api/resumes/${item.id}/download`}>
            Download
          </a>
        ) : (
          <span className="text-xs text-stone-500">No PDF file for this version</span>
        )}
        <button type="button" className={buttonClass} onClick={() => void copyPath()} disabled={!item.pdf_path}>
          Copy path
        </button>
        <button
          type="button"
          className={buttonClass}
          disabled={approve.isPending || item.run_status === 'failed'}
          onClick={() => approve.mutate({ id: item.id, approved: item.lifecycle !== 'approved' })}
        >
          {item.lifecycle === 'approved' ? 'Remove approval' : 'Mark approved'}
        </button>
        <button
          type="button"
          className={buttonClass}
          disabled={sent.isPending || item.run_status === 'failed'}
          onClick={() =>
            sent.mutate(item.id, {
              onSuccess: () => notify(`${item.code} marked as sent and attached to the tracker.`, 'info'),
              onError: (error) => notify(`Could not mark it sent: ${error.message}`, 'error'),
            })
          }
        >
          Mark sent
        </button>
        <button type="button" className={buttonClass} onClick={() => setRetailoring((v) => !v)} aria-expanded={retailoring}>
          Re-tailor
        </button>
        <button type="button" className={buttonClass} disabled={archive.isPending} onClick={() => archive.mutate({ id: item.id, archived: !item.archived })}>
          {item.archived ? 'Unarchive' : 'Archive'}
        </button>
        {item.sent_at ? <span className="text-xs text-stone-500">sent {formatDate(item.sent_at)}</span> : null}
      </div>
      {retailoring ? <RetailorConfirm job={{ id: item.job_id }} base={item.base} onDone={() => setRetailoring(false)} /> : null}

      {item.has_pdf ? (
        <iframe title={`Preview of ${item.code}`} src={`/api/resumes/${item.id}/preview`} className="h-[560px] w-full rounded border border-stone-200 dark:border-stone-700" />
      ) : null}

      {item.tailoring_strength ? <StrengthDetail strength={item.tailoring_strength} /> : null}

      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-stone-500">What changed vs the base resume ({baseLabel(item.base)})</h3>
        <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm">
          {item.change_text.map((line, index) => (
            <li key={index} className="break-words">
              {line}
            </li>
          ))}
        </ul>
        {item.diff_vs_previous ? <p className="mt-2 rounded bg-stone-100 px-2 py-1 text-sm dark:bg-stone-800">{item.diff_vs_previous}</p> : null}
      </div>

      <div className="grid gap-3 text-sm md:grid-cols-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-stone-500">Keywords</h3>
          <p className="text-emerald-800 dark:text-emerald-300">Matched: {item.keywords_matched.join(', ') || 'none recorded'}</p>
          <p className="text-amber-800 dark:text-amber-300">Missing (not on your resume): {item.keywords_missing.join(', ') || 'none'}</p>
        </div>
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-stone-500">Validator</h3>
          <ValidatorBadge status={item.validator_status} />
          {item.validation.violations && item.validation.violations.length > 0 ? (
            <ul className="mt-1 text-xs">
              {item.validation.violations.map((violation, index) => (
                <li key={index}>
                  <code>{violation.rule}</code>: {violation.offending}
                </li>
              ))}
            </ul>
          ) : null}
          {item.message ? <p className="mt-1 text-xs text-stone-500">{item.message}</p> : null}
        </div>
      </div>
    </section>
  )
}

export function ResumesPage() {
  const [params, setParams] = useSearchParams()
  const [company, setCompany] = useState('')
  const [status, setStatus] = useState('')
  const [base, setBase] = useState('')
  const [since, setSince] = useState('')
  const jobParam = params.get('job')
  const jobId = jobParam ? Number(jobParam) : undefined
  const [includeSuperseded, setIncludeSuperseded] = useState(params.get('all') === '1')
  const [includeArchived, setIncludeArchived] = useState(false)
  const list = useResumes({ company, status, base, since, includeSuperseded, includeArchived, jobId })
  const selectedParam = params.get('resume')
  const selected = selectedParam ? Number(selectedParam) : null

  const select = (id: number | null) => {
    const next = new URLSearchParams(params)
    if (id === null) next.delete('resume')
    else next.set('resume', String(id))
    setParams(next, { replace: false })
  }

  const [baseResumes, setBaseResumes] = useState<any[]>([])

  useEffect(() => {
    requestJson<{ bases: any[] }>('/api/resumes/bases')
      .then((res) => setBaseResumes(res.bases || []))
      .catch(() => setBaseResumes([]))
  }, [])

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold tracking-tight">Resumes</h1>
        <a className="text-xs text-brand-700 underline dark:text-brand-100" href="/api/resumes/export.csv">
          Export CSV
        </a>
      </div>
      <p className="text-xs text-stone-500">
        Every tailored version, newest first. Nothing here tailors or downloads by itself: Tailor and Re-tailor are
        explicit buttons, and the PDF is shown inline until you press Download. Resume text is never written by the model.
      </p>

      {baseResumes.length > 0 ? (
        <section aria-label="Base resumes" className="rounded-lg border border-stone-200 bg-white p-3 dark:border-stone-800 dark:bg-stone-900">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-stone-500 mb-2">Base Resumes & Capability</h2>
          <div className="grid gap-2 sm:grid-cols-2 md:grid-cols-3">
            {baseResumes.map((b) => (
              <div key={b.id} className="rounded border border-stone-100 p-2.5 dark:border-stone-800 bg-stone-50/50 dark:bg-stone-950/40 text-xs space-y-1">
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-stone-800 dark:text-stone-200">{baseLabel(b.id)}</span>
                  {b.template?.can_tailor ? (
                    <Chip tone="brand">Full tailoring</Chip>
                  ) : (
                    <Chip tone="neutral">Facts & letters only</Chip>
                  )}
                </div>
                <div className="text-stone-500 text-[11px]">
                  Template: <span className="font-medium text-stone-700 dark:text-stone-300">{b.template?.template || 'unknown'}</span>
                </div>
                <div className="text-stone-500 text-[11px] flex justify-between">
                  <span>{b.filename}</span>
                  <span>{b.pages ? `${b.pages} page${b.pages > 1 ? 's' : ''}` : ''}</span>
                </div>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      <form className="flex flex-wrap items-center gap-2" onSubmit={(event) => event.preventDefault()} aria-label="Resume filters">
        <input className={inputClass} placeholder="Company" aria-label="Company" value={company} onChange={(e) => setCompany(e.target.value)} />
        <select className={inputClass} aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">Any status</option>
          <option value="draft">Draft</option>
          <option value="approved">Approved</option>
          <option value="sent">Sent</option>
          <option value="superseded">Superseded</option>
        </select>
        <select className={inputClass} aria-label="Base" value={base} onChange={(e) => setBase(e.target.value)}>
          <option value="">Any base</option>
          <option value="ai">AI / ML</option>
          <option value="fullstack">Full-stack</option>
        </select>
        <label className="flex items-center gap-1 text-xs text-stone-600 dark:text-stone-300">
          Since <input type="date" className={inputClass} aria-label="Since date" value={since} onChange={(e) => setSince(e.target.value)} />
        </label>
        <label className="flex items-center gap-1 text-xs">
          <input type="checkbox" checked={includeSuperseded} onChange={(e) => setIncludeSuperseded(e.target.checked)} /> Show superseded
        </label>
        <label className="flex items-center gap-1 text-xs">
          <input type="checkbox" checked={includeArchived} onChange={(e) => setIncludeArchived(e.target.checked)} /> Show archived
        </label>
        {jobId !== undefined ? (
          <button type="button" className={buttonClass} onClick={() => setParams({})}>
            Clear job filter
          </button>
        ) : null}
      </form>

      {list.isPending ? <Spinner label="Loading resumes" /> : null}
      {list.isError ? <ErrorState message={list.error.message} onRetry={() => void list.refetch()} /> : null}
      {list.data && list.data.items.length === 0 ? (
        <EmptyState title="No resumes match">Tailor one from a job panel; it will appear here with its number.</EmptyState>
      ) : null}

      {list.data && list.data.items.length > 0 ? (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
          <div className="overflow-x-auto rounded-lg border border-stone-200 bg-white dark:border-stone-800 dark:bg-stone-900">
            <table className="w-full min-w-[980px] text-left text-sm">
              <thead>
                <tr className="text-xs uppercase tracking-wide text-stone-500">
                  {['Resume', 'Job', 'Created', 'Base', 'Recipe', 'Summary', 'Tailoring', 'Fill', 'Validator', 'Advice', 'Status', 'Tracker'].map((name) => (
                    <th key={name} className="px-2 py-2 font-medium">
                      {name}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {list.data.items.map((item) => (
                  <tr
                    key={item.id}
                    data-resume-id={item.id}
                    aria-selected={selected === item.id}
                    onClick={() => select(item.id)}
                    className={`cursor-pointer border-t border-stone-100 align-top hover:bg-stone-50 dark:border-stone-800 dark:hover:bg-stone-800/60 ${
                      selected === item.id ? 'bg-brand-50 dark:bg-brand-900/20' : ''
                    }`}
                  >
                    <td className="whitespace-nowrap px-2 py-2 font-medium">
                      <button type="button" className="underline" onClick={(e) => { e.stopPropagation(); select(item.id) }}>
                        {item.code}
                      </button>{' '}
                      v{item.version}
                    </td>
                    <td className="max-w-[260px] px-2 py-2">
                      <Link to={`/jobs?job=${item.job_id}`} onClick={(e) => e.stopPropagation()} className="block truncate hover:underline" title={item.job_title}>
                        {item.job_title}
                      </Link>
                      <span className="block truncate text-xs text-stone-500">{item.company_name}</span>
                      {item.apply_url ? (
                        <a href={item.apply_url} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()} className="text-xs text-brand-700 underline dark:text-brand-100">
                          Open posting ↗
                        </a>
                      ) : null}
                    </td>
                    <td className="whitespace-nowrap px-2 py-2 text-xs">{formatDate(item.created_at)}</td>
                    <td className="px-2 py-2 text-xs">{baseLabel(item.base)}</td>
                    <td className="px-2 py-2 text-xs">{item.recipe ?? '–'}</td>
                    <td className="px-2 py-2 text-xs">{item.summary_variant ?? '–'}</td>
                    <td className="px-2 py-2">
                      <StrengthChip strength={item.tailoring_strength} />
                    </td>
                    <td className="px-2 py-2 text-xs tabular-nums">{item.fill_ratio != null ? `${Math.round(item.fill_ratio * 100)}%` : '–'}</td>
                    <td className="px-2 py-2">
                      <ValidatorBadge status={item.validator_status} />
                    </td>
                    <td className="px-2 py-2 text-xs">
                      {item.advice_source === 'gemini' ? `Gemini $${item.cost_usd.toFixed(3)}` : 'deterministic'}
                    </td>
                    <td className="px-2 py-2">
                      <Chip tone={LIFECYCLE_TONE[item.lifecycle]}>{item.lifecycle}</Chip>
                    </td>
                    <td className="px-2 py-2 text-xs">
                      {item.application_id ? (
                        <Link to="/tracker" onClick={(e) => e.stopPropagation()} className="underline">
                          Used in application ({item.application_stage})
                        </Link>
                      ) : (
                        '–'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div>{selected !== null ? <Detail id={selected} /> : <EmptyState title="Select a resume">Click a row to preview it and see what changed.</EmptyState>}</div>
        </div>
      ) : null}
    </div>
  )
}
