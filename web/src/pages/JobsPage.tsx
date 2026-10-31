import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { queryKeys, useFacets, useJobs } from '../api/hooks'
import { requestJson } from '../api/client'
import type { JobDetail, JobView, SortKey } from '../api/types'
import { useDecisionActions } from '../components/DecisionDialogs'
import { FilterSidebar } from '../components/FilterSidebar'
import { JobDetailPanel } from '../components/JobDetailPanel'
import { JobList } from '../components/JobList'
import { EmptyState, ErrorState, SkeletonRows } from '../components/ui'
import type { JobFilters } from '../lib/filters'
import { DEFAULT_FILTERS, hasActiveFilters, parseFilters, recallFilters, rememberFilters, serializeFilters, urlHasControls } from '../lib/filters'

export const SORT_OPTIONS: { value: SortKey; label: string }[] = [
  { value: 'match', label: 'Best match' },
  { value: 'newest_posted', label: 'Newest posted' },
  { value: 'newest_seen', label: 'Newest first seen' },
  { value: 'oldest', label: 'Oldest' },
  { value: 'company', label: 'Company A–Z' },
  { value: 'title', label: 'Title A–Z' },
  { value: 'relevance', label: 'Best text match' },
]

const VIEW_TABS: { value: JobView; label: string }[] = [
  { value: 'open', label: 'Open' },
  { value: 'saved', label: 'Saved' },
  { value: 'applied', label: 'Applied' },
  { value: 'not_applying', label: 'Not applying' },
  { value: 'auto_hidden', label: 'Auto-hidden' },
  { value: 'all', label: 'All' },
]

export const SHORTCUTS: { keys: string; does: string }[] = [
  { keys: 'j / k', does: 'next / previous job' },
  { keys: 's', does: 'save the selected job' },
  { keys: 'a', does: 'open the Applied dialog' },
  { keys: 'x', does: 'open the Not applying dialog' },
  { keys: 'o', does: 'open the apply page' },
  { keys: '?', does: 'show this list' },
]

const OTHER_LOCATIONS_KEY = 'jobhunt.showOtherLocations'

function readOtherLocations(): boolean | null {
  try {
    const stored = window.localStorage.getItem(OTHER_LOCATIONS_KEY)
    return stored === null ? null : stored === '1'
  } catch {
    return null
  }
}

function rememberOtherLocations(value: boolean): void {
  try {
    window.localStorage.setItem(OTHER_LOCATIONS_KEY, value ? '1' : '0')
  } catch {
    /* private window: the choice simply is not remembered */
  }
}

function parseJobId(params: URLSearchParams): number | null {
  const raw = params.get('job')
  const id = raw ? Number.parseInt(raw, 10) : NaN
  return Number.isFinite(id) && id > 0 ? id : null
}

export function JobsPage() {
  const [params, setParams] = useSearchParams()
  const filters = useMemo(() => parseFilters(params), [params])
  const selectedJobId = parseJobId(params)

  const facets = useFacets()
  // "Show other locations": the URL wins, then the remembered choice, then the server's configured default.
  const effective = useMemo(
    () => ({ ...filters, otherLocations: filters.otherLocations ?? readOtherLocations() ?? facets.data?.show_other_locations_default ?? false }),
    [filters, facets.data],
  )
  const jobs = useJobs(effective)
  const [filtersOpen, setFiltersOpen] = useState(false)
  const actions = useDecisionActions()
  const queryClient = useQueryClient()
  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [helpOpen, setHelpOpen] = useState(false)

  // First visit without any of the five controls in the URL: bring back the remembered ones (the URL always wins).
  const restored = useRef(false)
  useEffect(() => {
    if (restored.current) return
    restored.current = true
    if (urlHasControls(params)) return
    const remembered = recallFilters()
    if (!remembered) return
    const next = serializeFilters({ ...filters, ...remembered })
    const job = params.get('job')
    if (job) next.set('job', job)
    setParams(next, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    if (restored.current) rememberFilters(filters)
  }, [filters])

  /** Write filters (and keep the open detail panel) into the URL. Changing a filter resets paging, never the sort. */
  const updateFilters = useCallback(
    (patch: Partial<JobFilters>, options?: { replace?: boolean }) => {
      if (patch.otherLocations !== undefined && patch.otherLocations !== null) rememberOtherLocations(patch.otherLocations)
      const next = { ...filters, ...patch, page: patch.page ?? 1 }
      const nextParams = serializeFilters(next)
      const job = params.get('job')
      if (job) nextParams.set('job', job)
      setParams(nextParams, { replace: options?.replace ?? false })
    },
    [filters, params, setParams],
  )

  const selectJob = useCallback(
    (id: number | null) => {
      const next = new URLSearchParams(params)
      if (id === null) next.delete('job')
      else next.set('job', String(id))
      setParams(next, { replace: id === null })
    },
    [params, setParams],
  )
  const closeDetail = useCallback(() => selectJob(null), [selectJob])

  // Search box: typing is debounced into the URL; Enter applies immediately.
  const [searchText, setSearchText] = useState(filters.q)
  const lastUrlQuery = useRef(filters.q)
  useEffect(() => {
    if (filters.q !== lastUrlQuery.current) {
      lastUrlQuery.current = filters.q
      setSearchText(filters.q) // back/forward or a shared link changed the query
    }
  }, [filters.q])
  useEffect(() => {
    if (searchText.trim() === filters.q.trim()) return
    const timer = window.setTimeout(() => {
      lastUrlQuery.current = searchText.trim()
      updateFilters({ q: searchText }, { replace: true })
    }, 300)
    return () => window.clearTimeout(timer)
  }, [searchText, filters.q, updateFilters])

  // Keyboard triage (only when no input is focused and no dialog is open): j/k move, s save, a Applied, x Not applying,
  // o open the apply page, ? help. Tailoring is deliberately NOT bound to any key: it is an explicit click only.
  const items = jobs.data?.items
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable)) return
      if (event.metaKey || event.ctrlKey || event.altKey || !items?.length) return
      if (document.querySelector('[role="dialog"]')) return
      const index = items.findIndex((item) => item.id === selectedJobId)
      const current = index >= 0 ? items[index] : undefined
      switch (event.key) {
        case 'j':
        case 'k': {
          const nextIndex = event.key === 'j' ? Math.min(items.length - 1, index + 1) : Math.max(0, index <= 0 ? 0 : index - 1)
          const next = items[nextIndex]
          if (next) selectJob(next.id)
          break
        }
        case 's':
          if (current) actions.save(current)
          break
        case 'a':
          if (current) actions.openApplied(current)
          break
        case 'x':
          if (current) actions.openNotApplying([current])
          break
        case 'o':
          if (current) {
            void queryClient
              .fetchQuery({ queryKey: queryKeys.job(current.id), queryFn: () => requestJson<JobDetail>(`/api/jobs/${current.id}`) })
              .then((detail) => {
                if (detail.apply_url) window.open(detail.apply_url, '_blank', 'noopener,noreferrer')
              })
              .catch(() => undefined)
          }
          break
        case '?':
          setHelpOpen((value) => !value)
          break
        default:
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [items, selectedJobId, selectJob, actions, queryClient])

  // ticked rows belong to the page they were ticked on
  useEffect(() => setChecked(new Set()), [jobs.data?.items, filters.view])
  const toggleChecked = useCallback(
    (id: number) =>
      setChecked((current) => {
        const next = new Set(current)
        if (next.has(id)) next.delete(id)
        else next.add(id)
        return next
      }),
    [],
  )

  const clearFilters = () => {
    setSearchText('')
    updateFilters({ ...DEFAULT_FILTERS, sort: filters.sort })
  }

  const totalPages = jobs.data ? Math.max(1, Math.ceil(jobs.data.total / jobs.data.page_size)) : 1

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-[240px_minmax(0,1fr)]">
      <aside className="lg:sticky lg:top-16 lg:max-h-[calc(100dvh-5rem)] lg:overflow-y-auto lg:pr-2">
        <button
          type="button"
          className="mb-3 w-full rounded-md border border-stone-300 px-3 py-2 text-left text-sm font-medium lg:hidden dark:border-stone-700"
          aria-expanded={filtersOpen}
          aria-controls="filters-panel"
          onClick={() => setFiltersOpen((value) => !value)}
        >
          Filters {hasActiveFilters(filters) ? '(active)' : ''}
        </button>
        <div id="filters-panel" className={filtersOpen ? 'block' : 'hidden lg:block'}>
          <FilterSidebar filters={effective} facets={facets.data} onChange={updateFilters} onClear={clearFilters} canClear={hasActiveFilters(filters)} />
        </div>
      </aside>

      <section aria-label="Results" className="min-w-0">
        <div className="mb-4 flex flex-wrap items-center gap-3">
          <form
            role="search"
            className="min-w-[220px] flex-1"
            onSubmit={(event) => {
              event.preventDefault()
              lastUrlQuery.current = searchText.trim()
              updateFilters({ q: searchText }, { replace: true })
            }}
          >
            <label htmlFor="job-search" className="sr-only">
              Search jobs
            </label>
            <input
              id="job-search"
              type="search"
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              placeholder="Search title, company, location, description…"
              autoComplete="off"
              className="w-full rounded-lg border border-stone-300 bg-white px-3.5 py-2 text-sm shadow-sm placeholder:text-stone-400 dark:border-stone-700 dark:bg-stone-900"
            />
          </form>
          <label className="flex items-center gap-2 text-sm">
            <span className="text-stone-500">Sort</span>
            <select
              value={filters.sort}
              onChange={(event) => updateFilters({ sort: event.target.value as SortKey })}
              className="rounded-md border border-stone-300 bg-white px-2 py-1.5 text-sm dark:border-stone-700 dark:bg-stone-900"
            >
              {SORT_OPTIONS.map((option) => (
                <option key={option.value} value={option.value} disabled={option.value === 'relevance' && !filters.q.trim()}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        <nav aria-label="View" className="mb-3 flex flex-wrap items-center gap-1.5">
          {VIEW_TABS.map((tab) => (
            <button
              key={tab.value}
              type="button"
              aria-pressed={filters.view === tab.value}
              onClick={() => updateFilters({ view: tab.value })}
              className={`rounded-full border px-3 py-1 text-sm font-medium ${
                filters.view === tab.value
                  ? 'border-stone-900 bg-stone-900 text-white dark:border-stone-100 dark:bg-stone-100 dark:text-stone-900'
                  : 'border-stone-300 hover:bg-stone-100 dark:border-stone-700 dark:hover:bg-stone-800'
              }`}
            >
              {tab.label}
            </button>
          ))}
          <button type="button" className="ml-auto text-xs text-stone-500 underline" onClick={() => setHelpOpen(true)}>
            Keyboard shortcuts (?)
          </button>
        </nav>
        {checked.size > 0 && jobs.data ? (
          <div role="region" aria-label="Bulk actions" className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-stone-300 bg-white px-3 py-2 text-sm dark:border-stone-700 dark:bg-stone-900">
            <strong>{checked.size}</strong> selected
            <button
              type="button"
              className="rounded-md bg-brand-700 px-3 py-1 font-medium text-white hover:bg-brand-800"
              onClick={() => actions.openNotApplying(jobs.data.items.filter((job) => checked.has(job.id)))}
            >
              Not applying…
            </button>
            <button type="button" className="underline" onClick={() => setChecked(new Set())}>
              Clear selection
            </button>
          </div>
        ) : null}
        <p className="mb-2 text-sm text-stone-500" aria-live="polite" data-testid="result-count">
          {jobs.data ? (
            <>
              <strong className="font-semibold text-stone-800 dark:text-stone-200">{jobs.data.total.toLocaleString()}</strong>{' '}
              {jobs.data.total === 1 ? 'job' : 'jobs'}
              {jobs.isFetching ? ' · updating…' : ''}
            </>
          ) : (
            'Loading…'
          )}
        </p>

        {jobs.isPending ? <SkeletonRows /> : null}
        {jobs.isError ? <ErrorState message={jobs.error.message} onRetry={() => void jobs.refetch()} /> : null}
        {jobs.data && jobs.data.items.length === 0 ? (
          <EmptyState title="No jobs match these filters">
            {hasActiveFilters(filters) ? (
              <button type="button" className="mt-2 rounded border border-stone-300 px-3 py-1 text-sm font-medium hover:bg-stone-100 dark:border-stone-700 dark:hover:bg-stone-800" onClick={clearFilters}>
                Clear filters
              </button>
            ) : (
              'Nothing has been ingested yet. Run `python -m jobhunt ingest`.'
            )}
          </EmptyState>
        ) : null}
        {jobs.data && jobs.data.items.length > 0 ? (
          <>
            <JobList jobs={jobs.data.items} selectedId={selectedJobId} onSelect={selectJob} checkedIds={checked} onToggleChecked={toggleChecked} />
            <nav aria-label="Pagination" className="mt-4 flex items-center justify-between text-sm">
              <button
                type="button"
                disabled={filters.page <= 1}
                onClick={() => updateFilters({ page: filters.page - 1 })}
                className="rounded-md border border-stone-300 px-3 py-1.5 enabled:hover:bg-stone-200 disabled:opacity-40 dark:border-stone-700 dark:enabled:hover:bg-stone-800"
              >
                ← Previous
              </button>
              <span className="text-stone-500">
                Page {filters.page} of {totalPages.toLocaleString()}
              </span>
              <button
                type="button"
                disabled={filters.page >= totalPages}
                onClick={() => updateFilters({ page: filters.page + 1 })}
                className="rounded-md border border-stone-300 px-3 py-1.5 enabled:hover:bg-stone-200 disabled:opacity-40 dark:border-stone-700 dark:enabled:hover:bg-stone-800"
              >
                Next →
              </button>
            </nav>
          </>
        ) : null}
      </section>

      {helpOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onMouseDown={() => setHelpOpen(false)}>
          <div role="dialog" aria-modal="true" aria-label="Keyboard shortcuts" className="w-full max-w-sm rounded-xl border border-stone-200 bg-white p-5 shadow-2xl dark:border-stone-700 dark:bg-stone-900" onMouseDown={(event) => event.stopPropagation()}>
            <h2 className="text-lg font-semibold">Keyboard shortcuts</h2>
            <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
              {SHORTCUTS.map((shortcut) => (
                <div key={shortcut.keys} className="contents">
                  <dt><kbd className="rounded border border-stone-300 px-1.5 dark:border-stone-600">{shortcut.keys}</kbd></dt>
                  <dd>{shortcut.does}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-3 text-xs text-stone-500">Tailoring has no shortcut on purpose: it is always an explicit click.</p>
            <button type="button" className="mt-3 rounded-md border border-stone-300 px-3 py-1 text-sm dark:border-stone-600" onClick={() => setHelpOpen(false)}>
              Close
            </button>
          </div>
        </div>
      ) : null}
      {selectedJobId !== null ? <JobDetailPanel key={selectedJobId} jobId={selectedJobId} onClose={closeDetail} /> : null}
    </div>
  )
}
