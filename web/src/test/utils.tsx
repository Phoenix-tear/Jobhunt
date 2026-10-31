import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { vi } from 'vitest'
import type { Application, BoardCard, JobDetail, JobSummary, Stage } from '../api/types'
import { DecisionProvider } from '../components/DecisionDialogs'
import { ToastProvider } from '../components/Toast'

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

export interface RecordedCall {
  method: string
  url: URL
  body: unknown
}

type Handler = (call: RecordedCall) => Response | unknown | Promise<Response | unknown>

/**
 * Replace global fetch with a router keyed by "METHOD /path". Unmatched requests fail the test
 * loudly (they would be a real network call in production).
 */
export function mockApi(handlers: Record<string, Handler>) {
  const calls: RecordedCall[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    const method = (init?.method ?? 'GET').toUpperCase()
    const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined
    const call = { method, url, body }
    calls.push(call)
    const handler = handlers[`${method} ${url.pathname}`]
    if (!handler) throw new Error(`unmocked request: ${method} ${url.pathname}${url.search}`)
    const result = await handler(call)
    return result instanceof Response ? result : jsonResponse(result)
  })
  vi.stubGlobal('fetch', fetchMock)
  return {
    calls,
    callsTo: (method: string, path: string) => calls.filter((call) => call.method === method && call.url.pathname === path),
    lastCall: (method: string, path: string) => calls.filter((call) => call.method === method && call.url.pathname === path).at(-1),
  }
}

export function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>
}

export function renderWithProviders(ui: ReactElement, route = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={[route]}>
          <DecisionProvider>
            {ui}
            <LocationProbe />
          </DecisionProvider>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

// --- fixtures -----------------------------------------------------------------------------------

export function makeJob(overrides: Partial<JobSummary> = {}): JobSummary {
  const id = overrides.id ?? 1
  return {
    id,
    title: `Software Engineer ${id}`,
    company_name: 'Acme',
    location: 'Bengaluru, India',
    city: 'Bengaluru',
    country: 'IN',
    is_remote: false,
    experience_level: 'entry',
    employment_type: 'full_time',
    employment_type_raw: 'full_time',
    posted_at: '2026-10-10T00:00:00Z',
    first_seen_at: '2026-10-10T00:00:00Z',
    listed_at: '2026-10-10T00:00:00Z',
    sources: ['greenhouse'],
    needs_description: false,
    status: 'open',
    saved: false,
    application_id: null,
    stage: null,
    match_score: null,
    gate_status: null,
    role_family: null,
    best_profile: null,
    llm_scored: false,
    workplace_type: 'unknown',
    remote_scope: 'unknown',
    remote_eligible_india: null,
    work_auth_required: false,
    feedback: null,
    exclusion_reason: null,
    seniority_level: null,
    seniority_evidence: null,
    location_fit: null,
    location_reason: null,
    location_matched: null,
    ...overrides,
  }
}

export function makeDetail(overrides: Partial<JobDetail> = {}): JobDetail {
  return {
    ...makeJob(overrides),
    description_text: 'Build things.',
    apply_url: 'https://boards.greenhouse.io/acme/jobs/1',
    sightings: [
      {
        source: 'greenhouse',
        source_job_id: '1',
        url: 'https://boards.greenhouse.io/acme/jobs/1',
        attribution_text: 'via greenhouse',
        first_seen_at: '2026-10-10T00:00:00Z',
        last_seen_at: '2026-10-12T00:00:00Z',
      },
    ],
    company: null,
    application: null,
    salary_min: null,
    salary_max: null,
    salary_currency: null,
    sponsorship_note: null,
    graduation_year_hint: null,
    last_seen_at: '2026-10-12T00:00:00Z',
    closed_at: null,
    match: null,
    ...overrides,
  }
}

export function makeApplication(id: number, stage: Stage, overrides: Partial<Application> = {}): Application {
  return {
    id,
    job_id: id * 10,
    stage,
    applied_at: null,
    notes: null,
    follow_up_at: null,
    created_at: '2026-10-10T00:00:00Z',
    updated_at: '2026-10-10T00:00:00Z',
    ...overrides,
  }
}

export function makeCard(id: number, stage: Stage, overrides: Partial<BoardCard> = {}): BoardCard {
  return {
    application: makeApplication(id, stage),
    title: `Role ${id}`,
    company_name: `Company ${id}`,
    location: 'Remote',
    country: null,
    apply_url: 'https://example.com/apply',
    job_status: 'open',
    ...overrides,
  }
}

export const EMPTY_FACETS = {
  total_open: 2,
  levels: [
    { value: 'intern', count: 1 },
    { value: 'new_grad', count: 1 },
  ],
  employment_types: [{ value: 'full_time', count: 2 }],
  countries: [{ value: 'IN', count: 2 }],
  sources: [{ value: 'greenhouse', count: 2 }],
  remote: 0,
  needs_description: 0,
  new_24h: 1,
  generated_at: '2026-10-12T00:00:00Z',
  scored: 0,
  relevant: 2,
  filtered_out: 5,
  workplace_types: [
    { value: 'hybrid', count: 3 },
    { value: 'onsite', count: 4 },
  ],
  remote_open_to_india: 1,
  gate_rules: [],
  location_fits: [
    { value: 'india', count: 3 },
    { value: 'wrong_location', count: 4 },
  ],
  show_other_locations_default: false,
}
