import { useEffect, useState } from 'react'
import { useAnswer, useCoverLetter, useKnockouts, useSaveEdit, useUseVersion, useWriting, useWritingEstimate } from '../api/hooks'
import type { JobDetail, WritingEstimate, WritingText } from '../api/types'
import { useToast } from './Toast'
import { Chip } from './ui'

function isOutline(item: WritingText): boolean {
  return item.source === 'outline' || item.source === 'template'
}

function wordCount(text: string): number {
  return text.trim() ? text.trim().split(/\s+/).length : 0
}

function ValidatorBadge({ item }: { item: WritingText }) {
  const ok = item.validation.ok !== false
  const rejected = item.rejected_violations.length
  const violations = item.validation.violations ?? []
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <span title={violations.map((v) => `${v.rule}: ${v.offending}`).join('\n')}>
        <Chip tone={ok ? 'brand' : 'warn'}>{ok ? 'facts validated ✓' : item.source === 'user_edit' ? 'edit adds claims' : 'validator failed'}</Chip>
      </span>
      {isOutline(item) ? <Chip tone="warn">outline (not a letter)</Chip> : null}
      {item.source === 'user_edit' ? <Chip>your edit</Chip> : null}
      {item.source === 'refusal' ? <Chip tone="warn">no text</Chip> : null}
      {rejected > 0 ? (
        <span title={item.rejected_violations.map((v) => `${v.rule}: ${v.offending}`).join('\n')}>
          <Chip tone="warn">model draft rejected ({rejected})</Chip>
        </span>
      ) : null}
    </span>
  )
}

function QualityBadge({ item }: { item: WritingText }) {
  const warnings = item.quality.warnings ?? []
  if (item.source === 'refusal' || item.quality.ok === undefined) return null
  return (
    <span title={warnings.map((w) => (w.detail ? `${w.rule}: ${w.detail}` : w.rule)).join('\n')}>
      <Chip tone={warnings.length === 0 ? 'brand' : 'warn'}>
        {warnings.length === 0 ? 'language checks ✓' : `${warnings.length} language warning${warnings.length === 1 ? '' : 's'}`}
      </Chip>
    </span>
  )
}

function StageCosts({ item }: { item: WritingText }) {
  if (item.stages.length === 0) {
    return <span>{item.cached ? 'cached' : item.model ? `${item.model} · $${item.cost_usd.toFixed(4)}` : 'no model call'}</span>
  }
  return (
    <span data-testid="stage-costs">
      {item.cached ? 'cached · ' : ''}
      {item.stages.map((stage, index) => (
        <span key={index} title={stage.note || (stage.accepted ? 'used' : 'not used')}>
          {index > 0 ? ' · ' : ''}
          {stage.stage} {stage.model} ${stage.cost_usd.toFixed(4)}
          {stage.accepted ? '' : ' (not used)'}
        </span>
      ))}
      {` · total $${item.cost_usd.toFixed(4)}`}
    </span>
  )
}

function EstimateLine({ estimate }: { estimate: WritingEstimate }) {
  return (
    <p className="text-xs text-stone-600 dark:text-stone-300" data-testid="estimate">
      About {estimate.calls} call{estimate.calls === 1 ? '' : 's'} (up to {estimate.max_calls}), roughly ${estimate.estimated_usd.toFixed(3)} (at most $
      {estimate.max_usd.toFixed(3)}). Interactive calls today: {estimate.interactive_used}/{estimate.interactive_limit}.
      {estimate.voice_samples ? ' Your writing samples are used as a style reference.' : ''}
      {estimate.blocked_reason ? <span className="text-amber-700 dark:text-amber-300"> Blocked: {estimate.blocked_reason}.</span> : null}
    </p>
  )
}

/** The explicit "yes, spend the calls" step: shows the estimate and the counter, then runs on confirm. */
function ConfirmRun({
  jobId,
  kind,
  label,
  busy,
  onConfirm,
  onCancel,
}: {
  jobId: number
  kind: 'cover_letter' | 'answer'
  label: string
  busy: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const estimate = useWritingEstimate(jobId, kind, true)
  return (
    <div className="space-y-1.5 rounded border border-stone-200 p-2 dark:border-stone-700" role="group" aria-label="Confirm model calls">
      {estimate.data ? <EstimateLine estimate={estimate.data} /> : <p className="text-xs text-stone-500">Estimating…</p>}
      <div className="flex gap-2 text-xs">
        <button type="button" disabled={busy || !estimate.data} onClick={onConfirm} className="rounded bg-brand-700 px-2 py-1 font-medium text-white hover:bg-brand-800 disabled:opacity-60">
          {busy ? 'Working…' : label}
        </button>
        <button type="button" onClick={onCancel} className="rounded border border-stone-300 px-2 py-1 hover:bg-stone-100 dark:border-stone-600 dark:hover:bg-stone-800">
          Cancel
        </button>
      </div>
    </div>
  )
}

function TextEditor({ items, jobId, onRegenerate, busy }: { items: WritingText[]; jobId: number; onRegenerate: () => void; busy: boolean }) {
  const { notify } = useToast()
  const saveEdit = useSaveEdit()
  const useVersion = useUseVersion()
  const [selected, setSelected] = useState<number | null>(null)
  const item = items.find((candidate) => candidate.id === selected) ?? items.find((candidate) => candidate.used_for_export) ?? (items[0] as WritingText)
  const [draft, setDraft] = useState(item.text)
  const [confirming, setConfirming] = useState(false)
  useEffect(() => {
    setDraft(item.text)
  }, [item.id, item.text])
  const dirty = draft !== item.text
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(draft)
      notify('Copied.', 'info')
    } catch {
      notify('Could not copy: select the text and copy it manually.', 'error')
    }
  }
  const limit = item.kind === 'cover_letter' ? '220-300 words' : null
  const warnings = item.quality.warnings ?? []
  const addedClaims = (item.validation.violations ?? []).filter((v) => v.rule === 'edit_adds_claim')
  const otherViolations = (item.validation.violations ?? []).filter((v) => v.rule !== 'edit_adds_claim')
  return (
    <div className="space-y-2 rounded-lg border border-stone-200 p-3 dark:border-stone-800" data-testid="writing-card" data-job={jobId}>
      <div className="flex flex-wrap items-center gap-2 text-xs text-stone-500">
        <ValidatorBadge item={item} />
        <QualityBadge item={item} />
        <span>
          {wordCount(draft)} words{limit ? ` (target ${limit})` : ''}
        </span>
        {items.length > 1 ? (
          <label className="inline-flex items-center gap-1">
            <span className="sr-only">Version</span>
            <select
              value={item.id ?? ''}
              onChange={(event) => setSelected(Number(event.target.value))}
              className="rounded border border-stone-300 bg-white px-1 py-0.5 dark:border-stone-600 dark:bg-stone-800"
              aria-label="Version"
            >
              {items.map((candidate) => (
                <option key={candidate.id ?? candidate.version} value={candidate.id ?? ''}>
                  v{candidate.version}
                  {candidate.source === 'user_edit' ? ' (your edit)' : ''}
                  {candidate.used_for_export ? ' · used for export' : ''}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <span>v{item.version}</span>
        )}
      </div>
      <div className="text-xs text-stone-500">
        <StageCosts item={item} />
      </div>
      {isOutline(item) ? (
        <p role="alert" className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-xs text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100">
          {item.source === 'template'
            ? 'This is an older automatic text, shown as a facts outline. It is not a cover letter and cannot be exported.'
            : `No letter was produced: ${item.message.split('no letter was produced: ')[1]?.split('; ')[0] ?? item.message}. Below is a facts outline for this job, not a cover letter; regenerate to try again.`}
        </p>
      ) : null}
      {item.question ? <p className="text-xs italic text-stone-500">Q: {item.question}</p> : null}
      <textarea
        aria-label={item.kind === 'cover_letter' ? 'Cover letter text' : 'Answer text'}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        rows={item.kind === 'cover_letter' ? 14 : 7}
        disabled={item.source === 'refusal' || isOutline(item)}
        className="block w-full resize-y rounded border border-stone-300 bg-white px-2 py-1.5 text-sm leading-relaxed dark:border-stone-600 dark:bg-stone-800"
      />
      {warnings.length > 0 ? (
        <ul aria-label="Language warnings" className="list-disc space-y-0.5 pl-5 text-xs text-amber-800 dark:text-amber-200">
          {warnings.slice(0, 8).map((warning, index) => (
            <li key={index}>
              {warning.rule.replace(/_/g, ' ')}
              {warning.detail ? `: ${warning.detail}` : ''}
            </li>
          ))}
        </ul>
      ) : null}
      {addedClaims.length > 0 ? (
        <p className="text-xs text-amber-800 dark:text-amber-200">
          Your edit adds numbers or technologies that are not in the cited facts: {addedClaims.map((v) => v.offending).join(', ')}. Check they are
          true and on your resume.
        </p>
      ) : null}
      {otherViolations.length > 0 ? (
        <ul aria-label="Copy and validator warnings" className="list-disc space-y-0.5 pl-5 text-xs text-amber-800 dark:text-amber-200">
          {otherViolations.slice(0, 8).map((violation, index) => (
            <li key={index}>
              {violation.rule.replace(/_/g, ' ')}: {violation.offending}
            </li>
          ))}
        </ul>
      ) : null}
      {item.message ? <p className="text-xs text-stone-500">{item.message}</p> : null}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <button type="button" onClick={() => void copy()} className="rounded border border-stone-300 px-2 py-1 hover:bg-stone-100 dark:border-stone-600 dark:hover:bg-stone-800">
          Copy
        </button>
        <button
          type="button"
          disabled={!dirty || saveEdit.isPending || item.id === null || item.source === 'refusal'}
          onClick={() => {
            if (item.id === null) return
            saveEdit.mutate(
              { textId: item.id, text: draft },
              {
                onSuccess: (saved) => {
                  setSelected(saved.id)
                  notify('Saved your edit as a new version (no model call).', 'info')
                },
                onError: (error) => notify(`Could not save: ${error.message}`, 'error'),
              },
            )
          }}
          className="rounded border border-stone-300 px-2 py-1 hover:bg-stone-100 disabled:opacity-50 dark:border-stone-600 dark:hover:bg-stone-800"
        >
          {saveEdit.isPending ? 'Saving…' : 'Save my edit'}
        </button>
        {!item.used_for_export && item.id !== null && item.source !== 'refusal' && items.length > 1 ? (
          <button
            type="button"
            disabled={useVersion.isPending}
            onClick={() => {
              if (item.id !== null) useVersion.mutate({ textId: item.id })
            }}
            className="rounded border border-stone-300 px-2 py-1 hover:bg-stone-100 disabled:opacity-50 dark:border-stone-600 dark:hover:bg-stone-800"
          >
            Use this version for export
          </button>
        ) : null}
        <button
          type="button"
          disabled={busy}
          onClick={() => setConfirming(true)}
          className="rounded border border-stone-300 px-2 py-1 hover:bg-stone-100 disabled:opacity-50 dark:border-stone-600 dark:hover:bg-stone-800"
        >
          Regenerate…
        </button>
        {item.kind === 'cover_letter' && item.id !== null && !isOutline(item) ? (
          <>
            <a className="text-brand-700 underline dark:text-brand-100" href={`/api/writing/${item.id}/export?format=txt`} download>
              .txt
            </a>
            <a className="text-brand-700 underline dark:text-brand-100" href={`/api/writing/${item.id}/export?format=pdf`} download>
              .pdf
            </a>
          </>
        ) : null}
        {dirty ? <span className="text-amber-700 dark:text-amber-300">unsaved changes: export uses the saved version</span> : null}
      </div>
      {confirming ? (
        <ConfirmRun
          jobId={jobId}
          kind={item.kind}
          label="Regenerate (new calls)"
          busy={busy}
          onConfirm={() => {
            onRegenerate()
            setConfirming(false)
          }}
          onCancel={() => setConfirming(false)}
        />
      ) : null}
    </div>
  )
}

/** Cover letter and "answer a question": both run only when clicked (after an estimate), counted per call, cached. */
export function WritingSection({ job }: { job: JobDetail }) {
  const writing = useWriting(job.id)
  const cover = useCoverLetter()
  const answer = useAnswer()
  const { notify } = useToast()
  const [question, setQuestion] = useState('')
  const [confirmFirst, setConfirmFirst] = useState(false)
  const knockouts = useKnockouts()
  const items = writing.data ?? []
  const letters = items.filter((item) => item.kind === 'cover_letter')
  const answerGroups = new Map<string, WritingText[]>()
  for (const item of items.filter((candidate) => candidate.kind === 'answer')) {
    const key = item.question ?? ''
    answerGroups.set(key, [...(answerGroups.get(key) ?? []), item])
  }

  const makeLetter = (regenerate: boolean) =>
    cover.mutate({ jobId: job.id, regenerate }, { onError: (error) => notify(`Could not write the letter: ${error.message}`, 'error') })
  const ask = (text: string, regenerate: boolean) =>
    answer.mutate({ jobId: job.id, question: text, regenerate }, { onError: (error) => notify(`Could not answer: ${error.message}`, 'error') })

  return (
    <section aria-label="Cover letter and answers" className="rounded-lg border border-stone-200 px-4 py-3 dark:border-stone-800">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-stone-500">Cover letter &amp; answers</h3>
      <p className="mt-1 text-xs text-stone-500">
        Uses only your ticked facts and this posting. A letter is planned, written and edited in separate calls (about three), each counted against the
        interactive limit; nothing runs until you confirm the estimate.
      </p>
      <div className="mt-2 space-y-3">
        {letters.length > 0 ? (
          <TextEditor items={letters} jobId={job.id} busy={cover.isPending} onRegenerate={() => makeLetter(true)} />
        ) : confirmFirst ? (
          <ConfirmRun jobId={job.id} kind="cover_letter" label="Write the letter" busy={cover.isPending} onConfirm={() => makeLetter(false)} onCancel={() => setConfirmFirst(false)} />
        ) : (
          <button type="button" disabled={cover.isPending} onClick={() => setConfirmFirst(true)} className="rounded-md bg-brand-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-800 disabled:opacity-60">
            {cover.isPending ? 'Writing…' : 'Cover letter'}
          </button>
        )}
        <div>
          <label className="block text-xs text-stone-500" htmlFor={`question-${job.id}`}>
            Answer a question (paste the application question)
          </label>
          <textarea
            id={`question-${job.id}`}
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            rows={3}
            maxLength={1200}
            placeholder="Why do you want to work here? (max 100 words)"
            className="mt-1 block w-full resize-y rounded border border-stone-300 bg-white px-2 py-1 text-sm dark:border-stone-600 dark:bg-stone-800"
          />
          <button
            type="button"
            disabled={answer.isPending || question.trim().length < 5}
            onClick={() => ask(question.trim(), false)}
            className="mt-1.5 rounded-md border border-stone-300 px-3 py-1.5 text-sm font-medium hover:bg-stone-100 disabled:opacity-50 dark:border-stone-600 dark:hover:bg-stone-800"
          >
            {answer.isPending ? 'Answering…' : 'Answer'}
          </button>
          <button
            type="button"
            disabled={knockouts.isPending || question.trim().length < 5}
            onClick={() =>
              knockouts.mutate(
                { jobId: job.id, questions: question.split(/\n+/).map((line) => line.trim()).filter(Boolean) },
                { onError: (error) => notify(`Could not check: ${error.message}`, 'error') },
              )
            }
            className="ml-2 mt-1.5 rounded-md border border-stone-300 px-3 py-1.5 text-sm font-medium hover:bg-stone-100 disabled:opacity-50 dark:border-stone-600 dark:hover:bg-stone-800"
          >
            {knockouts.isPending ? 'Checking…' : 'Check for knock-outs'}
          </button>
          <p className="mt-1 text-xs text-stone-500">
            Knock-out check: no model, one question per line. Compares with data/profile/application_answers.yaml; a blank value means “needs your
            confirmation”. Legal, demographic, visa, salary and disability questions are never drafted.
          </p>
          {knockouts.data ? (
            knockouts.data.length === 0 ? (
              <p className="mt-2 text-xs text-stone-500">No eligibility screens detected in those lines.</p>
            ) : (
              <ul aria-label="Knock-out results" className="mt-2 space-y-1.5">
                {knockouts.data.map((item, index) => (
                  <li key={index} className="rounded border border-stone-200 px-2 py-1.5 text-xs dark:border-stone-700">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Chip tone={item.risk === 'possible_knockout' ? 'warn' : item.risk === 'ok_by_your_value' ? 'brand' : 'neutral'}>
                        {item.risk === 'possible_knockout'
                          ? 'possible knock-out'
                          : item.risk === 'ok_by_your_value'
                            ? 'meets your value'
                            : 'needs your confirmation'}
                      </Chip>
                      <span className="text-stone-500">{item.kind.replace('_', ' ')}</span>
                      {item.your_value !== null ? <span>your value: {String(item.your_value)}</span> : null}
                    </div>
                    <p className="mt-1 break-words text-stone-600 dark:text-stone-300">{item.note}</p>
                    <p className="mt-0.5 break-words italic text-stone-500">Q: {item.question}</p>
                  </li>
                ))}
              </ul>
            )
          ) : null}
        </div>
        {[...answerGroups.entries()].slice(0, 5).map(([questionText, versions]) => (
          <TextEditor key={questionText} items={versions} jobId={job.id} busy={answer.isPending} onRegenerate={() => ask(questionText, true)} />
        ))}
      </div>
    </section>
  )
}
