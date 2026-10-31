import type { JobFilters } from '../lib/filters'

/** Non-2xx answer from the API (FastAPI error bodies carry `detail`). */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function describeDetail(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{loc: [...], msg: '...'}]
    return detail
      .map((entry) => (typeof entry === 'object' && entry !== null && 'msg' in entry ? String(entry.msg) : ''))
      .filter(Boolean)
      .join('; ') || fallback
  }
  return fallback
}

declare global {
  interface Window {
    __JOBHUNT_TOKEN__?: string
  }
}

let cachedToken: string | null = null

export async function getCsrfToken(): Promise<string> {
  if (typeof window !== 'undefined' && window.__JOBHUNT_TOKEN__) {
    return window.__JOBHUNT_TOKEN__
  }
  if (cachedToken) return cachedToken
  try {
    const res = await fetch('/api/bootstrap')
    if (res.ok) {
      const data = await res.json()
      cachedToken = data.csrf_token || ''
      return cachedToken!
    }
  } catch {
    // ignore
  }
  return ''
}

export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  const method = (init?.method || 'GET').toUpperCase()

  // Attach CSRF token on state-changing requests
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    if (!headers.has('X-Jobhunt-Token')) {
      const token = await getCsrfToken()
      if (token) headers.set('X-Jobhunt-Token', token)
    }
  }

  // Set Content-Type only if not FormData
  if (init?.body !== undefined && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  let response: Response
  try {
    response = await fetch(path, { ...init, headers })
  } catch {
    throw new ApiError(0, 'Cannot reach the jobhunt server. Is `python -m jobhunt serve` running?')
  }
  if (response.status === 204) return undefined as T
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body ? (body as { detail: unknown }).detail : null
    throw new ApiError(response.status, describeDetail(detail, `Request failed (${response.status})`))
  }
  return body as T
}

/** Query string for GET /api/jobs. Only non-default values are sent. */
export function jobsQueryString(filters: JobFilters, pageSize: number): string {
  const params = new URLSearchParams()
  if (filters.q.trim()) params.set('q', filters.q.trim())
  for (const value of filters.levels) params.append('level', value)
  for (const value of filters.types) params.append('employment_type', value)
  for (const value of filters.countries) params.append('country', value)
  for (const value of filters.sources) params.append('source', value)
  if (filters.remote) params.set('remote', 'true')
  for (const value of filters.workplace) params.append('workplace_type', value)
  if (filters.remoteIndia) params.set('remote_eligible_india', 'true')
  params.set('category', filters.category)
  for (const value of filters.freshness) params.append('freshness', value)
  if (filters.includeDismissed) params.set('include_dismissed', 'true')
  if (filters.juniorOnly) params.set('junior_only', 'true')
  for (const value of filters.locationFits) params.append('location_fit', value)
  if (filters.otherLocations === true) params.set('include_other_locations', 'true')
  if (filters.newWithin24h) params.set('new_within_hours', '24')
  if (filters.needsDescription) params.set('needs_description', 'true')
  if (!filters.hideSenior) params.set('hide_senior', 'false')
  if (filters.view !== 'open') params.set('view', filters.view)
  params.set('sort', filters.sort)
  if (filters.page > 1) params.set('page', String(filters.page))
  params.set('page_size', String(pageSize))
  return params.toString()
}
