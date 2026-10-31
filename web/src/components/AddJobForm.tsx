import { useState, type FormEvent } from 'react'
import { requestJson } from '../api/client'
import { useAddManualJob } from '../api/hooks'
import type { Application } from '../api/types'
import { useQueryClient } from '@tanstack/react-query'
import { useToast } from './Toast'

function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value.trim())
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

const ATS_MESSAGES: Record<string, string> = {
  fetched: 'The full posting was fetched from the employer’s job board.',
  not_found: 'The job board no longer lists this posting, so only your details were saved.',
  failed: 'The job board could not be reached right now; your details were saved.',
  skipped: 'Saved without contacting the job board.',
  not_applicable: '',
}

export function AddJobForm() {
  const add = useAddManualJob()
  const client = useQueryClient()
  const { notify } = useToast()
  const [url, setUrl] = useState('')
  const [title, setTitle] = useState('')
  const [company, setCompany] = useState('')
  const [location, setLocation] = useState('')
  const [description, setDescription] = useState('')
  const [fetchFromAts, setFetchFromAts] = useState(true)
  const [saveToTracker, setSaveToTracker] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setResult(null)
    if (!isHttpUrl(url)) {
      setError('Enter a full http(s) link, for example https://boards.greenhouse.io/acme/jobs/123.')
      return
    }
    if (!title.trim() || !company.trim()) {
      setError('Title and company are required.')
      return
    }
    setError(null)
    try {
      const outcome = await add.mutateAsync({
        title: title.trim(),
        company: company.trim(),
        apply_url: url.trim(),
        location: location.trim() || undefined,
        description: description.trim() || undefined,
        fetch_from_ats: fetchFromAts,
      })
      let message = outcome.created ? 'Job added.' : 'That link was already in the database; its record was updated.'
      const atsMessage = ATS_MESSAGES[outcome.ats.status] ?? ''
      if (atsMessage) message += ` ${atsMessage}`
      if (saveToTracker) {
        await requestJson<Application>('/api/applications', { method: 'POST', body: JSON.stringify({ job_id: outcome.job.id }) })
        await client.invalidateQueries({ queryKey: ['board'] })
        message += ' It is on your board in Saved.'
      }
      setResult(message)
      notify(message, 'info')
      setUrl('')
      setTitle('')
      setCompany('')
      setLocation('')
      setDescription('')
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Could not add the job.')
    }
  }

  const inputClass =
    'mt-1 block w-full rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-sm dark:border-stone-700 dark:bg-stone-900'

  return (
    <details className="rounded-lg border border-stone-200 bg-white dark:border-stone-800 dark:bg-stone-900">
      <summary className="cursor-pointer select-none px-4 py-2.5 text-sm font-medium">Add a job by URL</summary>
      <form onSubmit={(event) => void submit(event)} className="space-y-3 border-t border-stone-200 px-4 py-3 dark:border-stone-800" noValidate>
        <p className="text-xs text-stone-500">
          For a job you found somewhere else. The link is only stored, never opened. If it is a Greenhouse, Lever, Ashby, Workable,
          SmartRecruiters or Recruitee link, the posting itself can also be fetched from that board.
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block text-sm sm:col-span-2">
            Apply URL
            <input type="url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://…" className={inputClass} required />
          </label>
          <label className="block text-sm">
            Job title
            <input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={200} className={inputClass} required />
          </label>
          <label className="block text-sm">
            Company
            <input value={company} onChange={(event) => setCompany(event.target.value)} maxLength={200} className={inputClass} required />
          </label>
          <label className="block text-sm sm:col-span-2">
            Location <span className="text-stone-400">(optional)</span>
            <input value={location} onChange={(event) => setLocation(event.target.value)} maxLength={200} className={inputClass} />
          </label>
          <label className="block text-sm sm:col-span-2">
            Description <span className="text-stone-400">(optional, plain text)</span>
            <textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={4} className={inputClass} />
          </label>
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm">
          <label className="flex items-center gap-2">
            <input type="checkbox" className="size-4 accent-brand-700" checked={fetchFromAts} onChange={(event) => setFetchFromAts(event.target.checked)} />
            Fetch the posting if it is on a supported job board
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" className="size-4 accent-brand-700" checked={saveToTracker} onChange={(event) => setSaveToTracker(event.target.checked)} />
            Also put it on my board
          </label>
        </div>
        {error ? (
          <p role="alert" className="text-sm text-rose-700 dark:text-rose-300">
            {error}
          </p>
        ) : null}
        {result ? (
          <p role="status" className="text-sm text-emerald-700 dark:text-emerald-300">
            {result}
          </p>
        ) : null}
        <button
          type="submit"
          disabled={add.isPending}
          className="rounded-md bg-brand-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-800 disabled:opacity-60"
        >
          {add.isPending ? 'Adding…' : 'Add job'}
        </button>
      </form>
    </details>
  )
}
