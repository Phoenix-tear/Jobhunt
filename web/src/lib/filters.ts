import type { Category, JobView, SortKey } from '../api/types'

/**
 * Jobs-page filter state. The URL is the single source of truth: this module converts
 * URLSearchParams <-> JobFilters so searches are shareable and survive a refresh.
 * Defaults are omitted from the URL (category relevant, hideSenior on, sort match, page 1).
 *
 * Five independent controls decide what the list shows and in what order, and none of them resets another:
 * category (Relevant / Target only / Maybe / All / Filtered out), view (the tabs), location, freshness, and sort.
 * They are also remembered in localStorage (see rememberFilters / recallFilters).
 */
export type FreshnessChoice = 'fresh' | 'aging' | 'unknown'

export interface JobFilters {
  q: string
  levels: string[]
  types: string[]
  countries: string[]
  sources: string[]
  remote: boolean
  workplace: string[] // hybrid / onsite ("Remote only" is `remote`)
  remoteIndia: boolean
  category: Category
  freshness: FreshnessChoice[]
  includeDismissed: boolean
  juniorOnly: boolean
  locationFits: string[]
  otherLocations: boolean | null // null: use the server default
  newWithin24h: boolean
  needsDescription: boolean
  hideSenior: boolean
  view: JobView
  sort: SortKey
  page: number
}

export const DEFAULT_FILTERS: JobFilters = {
  q: '',
  levels: [],
  types: [],
  countries: [],
  sources: [],
  remote: false,
  workplace: [],
  remoteIndia: false,
  category: 'relevant',
  freshness: [],
  includeDismissed: false,
  juniorOnly: false,
  locationFits: [],
  otherLocations: null,
  newWithin24h: false,
  needsDescription: false,
  hideSenior: true,
  view: 'open',
  sort: 'match', // scored jobs first, then newest: identical to newest until something is scored
  page: 1,
}

export const VIEWS: readonly JobView[] = ['open', 'saved', 'applied', 'not_applying', 'auto_hidden', 'all']
export const SORTS: readonly SortKey[] = ['match', 'newest_posted', 'newest_seen', 'oldest', 'company', 'title', 'relevance', 'newest']
export const CATEGORIES: readonly Category[] = ['relevant', 'target', 'maybe', 'all', 'filtered_out']
export const FRESHNESS_CHOICES: readonly FreshnessChoice[] = ['fresh', 'aging', 'unknown']

function parseCategory(params: URLSearchParams): Category {
  const value = params.get('category')
  if (CATEGORIES.includes(value as Category)) return value as Category
  if (params.get('filtered') === '1') return 'filtered_out' // links from before the category control
  if (params.get('relevant') === '0') return 'all'
  return 'relevant'
}

export function parseFilters(params: URLSearchParams): JobFilters {
  const sort = params.get('sort')
  const page = Number.parseInt(params.get('page') ?? '1', 10)
  return {
    q: params.get('q') ?? '',
    levels: params.getAll('level'),
    types: params.getAll('type'),
    countries: params.getAll('country'),
    sources: params.getAll('source'),
    remote: params.get('remote') === '1',
    workplace: params.getAll('wp').filter((value) => value === 'hybrid' || value === 'onsite'),
    remoteIndia: params.get('india') === '1',
    category: parseCategory(params),
    freshness: params.getAll('fresh').filter((value): value is FreshnessChoice => FRESHNESS_CHOICES.includes(value as FreshnessChoice)),
    includeDismissed: params.get('dismissed') === '1',
    juniorOnly: params.get('junior') === '1',
    locationFits: params.getAll('loc'),
    otherLocations: params.get('other') === '1' ? true : params.get('other') === '0' ? false : null,
    newWithin24h: params.get('new24') === '1',
    needsDescription: params.get('nodesc') === '1',
    hideSenior: params.get('hideSenior') !== '0',
    view: VIEWS.includes(params.get('view') as JobView) ? (params.get('view') as JobView) : 'open',
    sort: SORTS.includes(sort as SortKey) ? (sort as SortKey) : 'match',
    page: Number.isFinite(page) && page > 1 ? page : 1,
  }
}

/** Serialize to URL params; `job` (the open detail panel) is preserved by the caller. */
export function serializeFilters(filters: JobFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.q.trim()) params.set('q', filters.q.trim())
  for (const value of filters.levels) params.append('level', value)
  for (const value of filters.types) params.append('type', value)
  for (const value of filters.countries) params.append('country', value)
  for (const value of filters.sources) params.append('source', value)
  if (filters.remote) params.set('remote', '1')
  for (const value of filters.workplace) params.append('wp', value)
  if (filters.remoteIndia) params.set('india', '1')
  if (filters.category !== DEFAULT_FILTERS.category) params.set('category', filters.category)
  for (const value of filters.freshness) params.append('fresh', value)
  if (filters.includeDismissed) params.set('dismissed', '1')
  if (filters.juniorOnly) params.set('junior', '1')
  for (const value of filters.locationFits) params.append('loc', value)
  if (filters.otherLocations !== null) params.set('other', filters.otherLocations ? '1' : '0')
  if (filters.newWithin24h) params.set('new24', '1')
  if (filters.needsDescription) params.set('nodesc', '1')
  if (!filters.hideSenior) params.set('hideSenior', '0')
  if (filters.view !== 'open') params.set('view', filters.view)
  if (filters.sort !== DEFAULT_FILTERS.sort) params.set('sort', filters.sort)
  if (filters.page > 1) params.set('page', String(filters.page))
  return params
}

export function toggleValue(values: string[], value: string): string[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value]
}

export function hasActiveFilters(filters: JobFilters): boolean {
  const defaults = DEFAULT_FILTERS
  return (
    filters.q.trim() !== '' ||
    filters.levels.length > 0 ||
    filters.types.length > 0 ||
    filters.countries.length > 0 ||
    filters.sources.length > 0 ||
    filters.remote !== defaults.remote ||
    filters.workplace.length > 0 ||
    filters.remoteIndia ||
    filters.category !== defaults.category ||
    filters.freshness.length > 0 ||
    filters.includeDismissed ||
    filters.juniorOnly ||
    filters.locationFits.length > 0 ||
    filters.otherLocations === true ||
    filters.newWithin24h !== defaults.newWithin24h ||
    filters.needsDescription !== defaults.needsDescription ||
    filters.hideSenior !== defaults.hideSenior ||
    filters.view !== defaults.view
  )
}


// --- remembering the five controls ---------------------------------------------------------------------------------------

export const STORAGE_KEY = 'jobhunt.jobFilters'
type Remembered = Pick<JobFilters, 'category' | 'view' | 'locationFits' | 'freshness' | 'sort'>

/** Save category, view, location, freshness and sort. Never throws (private windows / blocked storage). */
export function rememberFilters(filters: JobFilters): void {
  try {
    const value: Remembered = { category: filters.category, view: filters.view, locationFits: filters.locationFits, freshness: filters.freshness, sort: filters.sort }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value))
  } catch {
    /* the choice simply is not remembered */
  }
}

/** The remembered controls as URL params (only valid values), or null when nothing usable is stored. */
export function recallFilters(): Partial<Remembered> | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const data = JSON.parse(raw) as Partial<Remembered>
    const out: Partial<Remembered> = {}
    if (CATEGORIES.includes(data.category as Category)) out.category = data.category
    if (VIEWS.includes(data.view as JobView)) out.view = data.view
    if (SORTS.includes(data.sort as SortKey)) out.sort = data.sort
    if (Array.isArray(data.locationFits)) out.locationFits = data.locationFits.filter((item) => typeof item === 'string')
    if (Array.isArray(data.freshness)) out.freshness = data.freshness.filter((item): item is FreshnessChoice => FRESHNESS_CHOICES.includes(item as FreshnessChoice))
    return Object.keys(out).length ? out : null
  } catch {
    return null
  }
}

/** True when the URL already says something about the five controls (then the URL wins over what was remembered). */
export function urlHasControls(params: URLSearchParams): boolean {
  return ['category', 'view', 'sort', 'loc', 'fresh', 'relevant', 'filtered'].some((key) => params.has(key))
}
