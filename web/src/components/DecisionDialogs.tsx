import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useAppliedDecision, useBulkNotApplying, useCompanyRuleActions, useNotApplying, useResumes, useSaveDecision, useUndoDecision } from '../api/hooks'
import { APPLY_HOW, APPLY_HOW_LABELS, NOT_APPLYING_LABELS, NOT_APPLYING_REASONS } from '../api/types'
import type { ApplyHow, CompanyRulePreview, NotApplyingReason } from '../api/types'
import { useToast } from './Toast'
import { Chip } from './ui'

export interface DecidableJob {
  id: number
  title: string
  company_name?: string
  decision?: 'saved' | 'applied' | 'not_applying' | null
  decision_reason?: string | null
  likely_ineligible?: string | null
}

interface DecisionApi {
  save: (job: DecidableJob) => void
  openApplied: (job: DecidableJob) => void
  openNotApplying: (jobs: DecidableJob[]) => void
  undo: (job: DecidableJob) => void
}

const noop = () => undefined
const DecisionContext = createContext<DecisionApi>({ save: noop, openApplied: noop, openNotApplying: noop, undo: noop })

export function useDecisionActions(): DecisionApi {
  return useContext(DecisionContext)
}

const LEGACY_TO_NEW: Record<string, NotApplyingReason> = { wrong_stack: 'wrong_domain_or_stack', company: 'company_not_interested' }
export function reasonLabel(reason: string | null | undefined): string {
  if (!reason) return ''
  const normalized = (LEGACY_TO_NEW[reason] ?? reason) as NotApplyingReason
  return NOT_APPLYING_LABELS[normalized] ?? reason
}

const dialogClass = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4'
const panelClass = 'max-h-[90dvh] w-full max-w-md overflow-y-auto rounded-xl border border-stone-200 bg-white p-5 shadow-2xl dark:border-stone-700 dark:bg-stone-900'
const fieldClass = 'mt-1 block w-full rounded border border-stone-300 bg-white px-2 py-1.5 text-sm dark:border-stone-600 dark:bg-stone-800'
const primaryButton = 'rounded-md bg-brand-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-800 disabled:opacity-60'
const plainButton = 'rounded-md border border-stone-300 px-3 py-1.5 text-sm font-medium hover:bg-stone-100 dark:border-stone-600 dark:hover:bg-stone-800'

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onClose])
  return (
    <div className={dialogClass} onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div role="dialog" aria-modal="true" aria-label={title} className={panelClass}>
        <h2 className="text-lg font-semibold">{title}</h2>
        {children}
      </div>
    </div>
  )
}

function todayInputValue(): string {
  const now = new Date()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
}

function appliedAtIso(dateValue: string): string {
  return dateValue === todayInputValue() ? new Date().toISOString() : `${dateValue}T12:00:00.000Z`
}

function AppliedDialog({ job, onClose, onConfirm, busy }: {
  job: DecidableJob
  onClose: () => void
  busy: boolean
  onConfirm: (input: { applied_at: string; how: ApplyHow; resume_id: number | null; note: string }) => void
}) {
  const versions = useResumes({ jobId: job.id, includeSuperseded: true })
  const items = (versions.data?.items ?? []).filter((item) => item.run_status !== 'failed')
  const [date, setDate] = useState(todayInputValue())
  const [how, setHow] = useState<ApplyHow>('company_site')
  const [note, setNote] = useState('')
  const [resume, setResume] = useState<string | null>(null)
  const defaultResume = (items.find((item) => item.lifecycle === 'approved') ?? null)?.id
  const chosen = resume ?? (defaultResume !== undefined ? String(defaultResume) : '')
  return (
    <Modal title={`Applied: ${job.title}`} onClose={onClose}>
      <p className="mt-1 text-xs text-stone-500">{job.company_name}. This moves the tracker entry to Applied, sets a follow-up in 7 days and marks the chosen resume as sent.</p>
      <label className="mt-3 block text-sm">
        Date applied
        <input type="date" className={fieldClass} value={date} max={todayInputValue()} onChange={(event) => setDate(event.target.value)} />
      </label>
      <label className="mt-3 block text-sm">
        How
        <select className={fieldClass} value={how} onChange={(event) => setHow(event.target.value as ApplyHow)}>
          {APPLY_HOW.map((option) => (
            <option key={option} value={option}>
              {APPLY_HOW_LABELS[option]}
            </option>
          ))}
        </select>
      </label>
      <label className="mt-3 block text-sm">
        Resume version
        <select className={fieldClass} value={chosen} onChange={(event) => setResume(event.target.value)}>
          <option value="">None / other</option>
          {items.map((item) => (
            <option key={item.id} value={item.id}>
              {item.code} v{item.version} ({item.lifecycle})
            </option>
          ))}
        </select>
      </label>
      <label className="mt-3 block text-sm">
        Note (optional)
        <textarea className={fieldClass} rows={2} maxLength={1000} value={note} onChange={(event) => setNote(event.target.value)} />
      </label>
      <div className="mt-4 flex justify-end gap-2">
        <button type="button" className={plainButton} onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className={primaryButton}
          disabled={busy || !date}
          onClick={() => onConfirm({ applied_at: appliedAtIso(date), how, resume_id: chosen ? Number(chosen) : null, note })}
        >
          {busy ? 'Saving…' : 'Confirm applied'}
        </button>
      </div>
    </Modal>
  )
}

/** What "Remember for this company" will write; shown in full before the confirming click. */
export interface RememberRule {
  jobId: number
  pattern: string
  text: string
}

function RememberForCompany({ job, onChange }: { job: DecidableJob; onChange: (rule: RememberRule | null) => void }) {
  const { preview } = useCompanyRuleActions()
  const [checked, setChecked] = useState(false)
  const [shown, setShown] = useState<CompanyRulePreview | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const toggle = (value: boolean) => {
    setChecked(value)
    setShown(null)
    setProblem(null)
    onChange(null)
    if (!value) return
    preview.mutate(
      { jobId: job.id },
      {
        onSuccess: (data) => {
          setShown(data)
          if (!data.already_denied) onChange({ jobId: job.id, pattern: data.pattern, text: data.text })
        },
        onError: (error) => setProblem(error.message),
      },
    )
  }
  return (
    <div className="mt-2 rounded-md border border-stone-200 px-3 py-2 text-sm dark:border-stone-700">
      <label className="flex items-start gap-2">
        <input type="checkbox" className="mt-1" checked={checked} onChange={(event) => toggle(event.target.checked)} />
        <span>Remember for this company</span>
      </label>
      {checked && preview.isPending ? <p className="mt-1 text-xs text-stone-500">Looking for the level word in the title…</p> : null}
      {problem ? <p className="mt-1 text-xs text-amber-800 dark:text-amber-300">Nothing to remember: {problem}.</p> : null}
      {shown ? (
        <div className="mt-1.5 text-xs">
          {shown.already_denied ? (
            <p>“{shown.pattern}” is already denied for {shown.company}; nothing will be written.</p>
          ) : (
            <>
              <p>
                Confirming below appends exactly this to <code>{shown.path}</code> (it takes effect at the next relevance run; you can remove it
                from Health → Company rules):
              </p>
              <pre data-testid="rule-preview" className="mt-1 overflow-x-auto rounded bg-stone-100 p-2 text-[11px] dark:bg-stone-800">{shown.text.trim()}</pre>
            </>
          )}
        </div>
      ) : null}
    </div>
  )
}

function NotApplyingDialog({ jobs, onClose, onConfirm, busy }: {
  jobs: DecidableJob[]
  onClose: () => void
  busy: boolean
  onConfirm: (reason: NotApplyingReason, note: string, remember: RememberRule | null) => void
}) {
  const [remember, setRemember] = useState<RememberRule | null>(null)
  const current = jobs.length === 1 ? jobs[0]?.decision_reason : null
  const preselected = current ? ((LEGACY_TO_NEW[current] ?? current) as NotApplyingReason) : null
  const [reason, setReason] = useState<NotApplyingReason | null>(NOT_APPLYING_REASONS.includes(preselected as NotApplyingReason) ? preselected : null)
  const [note, setNote] = useState('')
  const title = jobs.length === 1 ? `Not applying: ${jobs[0]?.title ?? ''}` : `Not applying to ${jobs.length} jobs`
  return (
    <Modal title={title} onClose={onClose}>
      <p className="mt-1 text-xs text-stone-500">
        Pick one reason. The job leaves your lists but stays searchable under “Not applying”. Role-fit reasons (wrong role,
        too senior, wrong domain) also teach the ranker; the other reasons do not.
      </p>
      {jobs.length === 1 && jobs[0]?.likely_ineligible ? <p className="mt-2 text-xs text-amber-800 dark:text-amber-300">{jobs[0].likely_ineligible}</p> : null}
      <fieldset className="mt-3 space-y-1.5">
        <legend className="sr-only">Reason</legend>
        {NOT_APPLYING_REASONS.map((option) => (
          <label key={option} className="flex items-start gap-2 text-sm">
            <input type="radio" name="not-applying-reason" value={option} checked={reason === option} onChange={() => setReason(option)} className="mt-1" />
            <span>{NOT_APPLYING_LABELS[option]}</span>
          </label>
        ))}
      </fieldset>
      {reason === 'ineligible_level' && jobs.length === 1 && jobs[0] ? <RememberForCompany key={jobs[0].id} job={jobs[0]} onChange={setRemember} /> : null}
      {reason === 'closed_or_unavailable' ? (
        <p className="mt-2 text-xs text-stone-500">The job is also closed locally, so it stops being scored. Undo reopens it. Each report feeds the per-source report on Health.</p>
      ) : null}
      <label className="mt-3 block text-sm">
        Note (optional)
        <textarea className={fieldClass} rows={2} maxLength={1000} value={note} onChange={(event) => setNote(event.target.value)} />
      </label>
      <div className="mt-4 flex justify-end gap-2">
        <button type="button" className={plainButton} onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className={primaryButton}
          disabled={busy || reason === null}
          onClick={() => reason && onConfirm(reason, note, reason === 'ineligible_level' ? remember : null)}
        >
          {busy ? 'Saving…' : remember ? 'Confirm and write this line' : 'Confirm'}
        </button>
      </div>
    </Modal>
  )
}

/** Hosts the Applied / Not applying dialogs once for the whole app and exposes the actions (with Undo toasts). */
export function DecisionProvider({ children }: { children: ReactNode }) {
  const { notify } = useToast()
  const saveMutation = useSaveDecision()
  const appliedMutation = useAppliedDecision()
  const notApplyingMutation = useNotApplying()
  const bulkMutation = useBulkNotApplying()
  const ruleActions = useCompanyRuleActions()
  const undoMutation = useUndoDecision()
  const [applied, setApplied] = useState<DecidableJob | null>(null)
  const [notApplying, setNotApplying] = useState<DecidableJob[] | null>(null)
  const undoRef = useRef<(job: DecidableJob) => void>(noop)

  const undo = useCallback(
    (job: DecidableJob) =>
      undoMutation.mutate(job.id, {
        onSuccess: () => notify(`Undone for “${job.title}”.`, 'info'),
        onError: (error) => notify(`Could not undo: ${error.message}`, 'error'),
      }),
    [undoMutation, notify],
  )
  undoRef.current = undo

  const save = useCallback(
    (job: DecidableJob) =>
      saveMutation.mutate(job.id, {
        onSuccess: () => notify(`Saved “${job.title}”.`, 'info', { label: 'Undo', onClick: () => undoRef.current(job) }),
        onError: (error) => notify(`Could not save: ${error.message}`, 'error'),
      }),
    [saveMutation, notify],
  )

  const api = useMemo<DecisionApi>(
    () => ({ save, openApplied: setApplied, openNotApplying: setNotApplying, undo }),
    [save, undo],
  )

  return (
    <DecisionContext.Provider value={api}>
      {children}
      {applied ? (
        <AppliedDialog
          job={applied}
          busy={appliedMutation.isPending}
          onClose={() => setApplied(null)}
          onConfirm={(input) =>
            appliedMutation.mutate(
              { jobId: applied.id, applied_at: input.applied_at, how: input.how, resume_id: input.resume_id, note: input.note || undefined },
              {
                onSuccess: () => {
                  notify(`Marked “${applied.title}” as applied. Follow-up in 7 days.`, 'info', { label: 'Undo', onClick: () => undoRef.current(applied) })
                  setApplied(null)
                },
                onError: (error) => notify(`Could not record it: ${error.message}`, 'error'),
              },
            )
          }
        />
      ) : null}
      {notApplying ? (
        <NotApplyingDialog
          jobs={notApplying}
          busy={notApplyingMutation.isPending || bulkMutation.isPending}
          onClose={() => setNotApplying(null)}
          onConfirm={(reason, note, remember) => {
            const jobs = notApplying
            const undoAll = () => jobs.forEach((job) => undoRef.current(job))
            const success = () => {
              notify(
                jobs.length === 1 ? `Not applying to “${jobs[0]?.title}”: ${NOT_APPLYING_LABELS[reason]}.` : `Not applying to ${jobs.length} jobs: ${NOT_APPLYING_LABELS[reason]}.`,
                'info',
                { label: 'Undo', onClick: undoAll },
              )
              setNotApplying(null)
              if (remember) {
                // the only place the rules file is written: the click that was shown this exact text
                ruleActions.write.mutate(remember, {
                  onSuccess: () => notify(`Remembered “${remember.pattern}” for ${jobs[0]?.company_name ?? 'this company'}. It applies at the next relevance run.`, 'info'),
                  onError: (error) => notify(`The decision was saved, but the rule was not written: ${error.message}`, 'error'),
                })
              }
            }
            const failure = (error: Error) => notify(`Could not save the decision: ${error.message}`, 'error')
            if (jobs.length === 1 && jobs[0]) {
              notApplyingMutation.mutate({ jobId: jobs[0].id, reason, note: note || undefined }, { onSuccess: success, onError: failure })
            } else {
              bulkMutation.mutate({ jobIds: jobs.map((job) => job.id), reason, note: note || undefined }, { onSuccess: success, onError: failure })
            }
          }}
        />
      ) : null}
    </DecisionContext.Provider>
  )
}

/** State chip plus the Save / Applied / Not applying buttons for one job (panel and rows). */
export function DecisionControls({ job, compact = false }: { job: DecidableJob; compact?: boolean }) {
  const actions = useDecisionActions()
  const size = compact ? 'px-2 py-0.5 text-xs' : 'px-3 py-1.5 text-sm'
  const button = `rounded-md border border-stone-300 font-medium hover:bg-stone-100 dark:border-stone-600 dark:hover:bg-stone-800 ${size}`
  const decision = job.decision
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5" data-testid="decision-controls">
      {decision === 'saved' ? <Chip tone="brand">Saved</Chip> : null}
      {decision === 'applied' ? <Chip tone="brand">Applied</Chip> : null}
      {decision === 'not_applying' ? <Chip tone="warn">Not applying: {reasonLabel(job.decision_reason)}</Chip> : null}
      {!decision ? (
        <button type="button" className={button} onClick={() => actions.save(job)}>
          Save
        </button>
      ) : null}
      {decision !== 'applied' ? (
        <button type="button" className={button} onClick={() => actions.openApplied(job)}>
          Applied
        </button>
      ) : null}
      <button type="button" className={button} onClick={() => actions.openNotApplying([job])}>
        {decision === 'not_applying' ? 'Change reason' : 'Not applying'}
      </button>
      {decision ? (
        <button type="button" className={button} onClick={() => actions.undo(job)}>
          Undo
        </button>
      ) : null}
    </span>
  )
}
