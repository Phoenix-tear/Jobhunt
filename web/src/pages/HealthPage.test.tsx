import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Health } from '../api/types'
import { jsonResponse, mockApi, renderWithProviders } from '../test/utils'
import { HealthPage } from './HealthPage'

afterEach(() => {
  vi.unstubAllGlobals()
})

const healthy = (overrides: Partial<Health> = {}): Health => ({
  matching: null,
  generated_at: '2026-10-12T10:00:00Z',
  jobs_open: 17728,
  jobs_closed: 2630,
  companies_active: 2131,
  companies_unverified: 7144,
  companies_inactive: 0,
  applications_by_stage: { saved: 3, applied: 1 },
  sources: [
    {
      source: 'greenhouse', kind: 'ats', enabled: true, last_run_at: '2026-10-12T09:00:00Z', last_success_at: '2026-10-12T09:00:00Z',
      age_hours: 1, stale: false, never_run: false, fetch_count_today: 2121,
      last_run: { started_at: '2026-10-12T08:50:00Z', finished_at: '2026-10-12T09:00:00Z', companies_checked: 2121, jobs_seen: 87657, jobs_new: 9275, jobs_closed: 723, jobs_skipped_by_prefilter: 77067, error_count: 0, first_error: null },
    },
    {
      source: 'remotive', kind: 'feed', enabled: true, last_run_at: '2026-10-05T09:00:00Z', last_success_at: '2026-10-05T09:00:00Z',
      age_hours: 193, stale: true, never_run: false, fetch_count_today: 0,
      last_run: { started_at: '2026-10-05T09:00:00Z', finished_at: '2026-10-05T09:01:00Z', companies_checked: 0, jobs_seen: 15, jobs_new: 1, jobs_closed: 0, jobs_skipped_by_prefilter: 14, error_count: 2, first_error: 'remotive: transient: HTTP 503' },
    },
    {
      source: 'adzuna', kind: 'feed', enabled: true, last_run_at: null, last_success_at: null,
      age_hours: null, stale: false, never_run: true, fetch_count_today: 0, last_run: null,
    },
  ],
  gmail: { credentials_file_exists: false, token_file_exists: false, scopes: [], read_only_scope: false, account_configured: 'you@example.com' },
  user_agent_contact_set: false,
  schema_version: 2,
  warnings: [
    { code: 'user_agent_contact_missing', message: 'user_agent_contact is empty in config.yaml: requests to job boards carry no way to reach you.' },
    { code: 'gmail_token_missing', message: 'No Gmail token found: run `python -m jobhunt gmail-auth` (see SETUP.md) to ingest alert emails.' },
    { code: 'source_stale', message: 'remotive has not succeeded for 193 h (threshold 72 h).' },
  ],
  ...overrides,
})

describe('HealthPage', () => {
  it('lists warnings with hints and the per-source status', async () => {
    mockApi({ 'GET /api/health': () => healthy() })
    renderWithProviders(<HealthPage />, '/health')

    const warnings = await screen.findByRole('list', { name: 'Warnings' })
    expect(within(warnings).getAllByRole('alert')).toHaveLength(3)
    expect(within(warnings).getByText(/user_agent_contact is empty/)).toBeInTheDocument()
    expect(within(warnings).getByText(/No Gmail token found/)).toBeInTheDocument()
    expect(within(warnings).getByText(/remotive has not succeeded for 193 h/)).toBeInTheDocument()
    expect(within(warnings).getByText('Set user_agent_contact in config.yaml (a URL or email).')).toBeInTheDocument()

    const rows = screen.getByRole('region', { name: 'Sources' })
    expect(within(rows).getByText('Stale')).toBeInTheDocument()
    expect(within(rows).getByText('Never run')).toBeInTheDocument()
    expect(within(rows).getByText('OK')).toBeInTheDocument()
    expect(within(rows).getByText(/87,657 seen · 9,275 new · 723 closed/)).toBeInTheDocument()
    expect(within(rows).getByText(/2 errors/)).toBeInTheDocument()
    expect(screen.getByText('token.json missing (run gmail-auth)')).toBeInTheDocument()
    expect(screen.getByText('user_agent_contact is empty')).toBeInTheDocument()
  })

  it('shows the all-clear message when there is nothing to warn about', async () => {
    mockApi({
      'GET /api/health': () =>
        healthy({
          warnings: [],
          user_agent_contact_set: true,
          gmail: { credentials_file_exists: true, token_file_exists: true, scopes: ['https://www.googleapis.com/auth/gmail.readonly'], read_only_scope: true, account_configured: null },
        }),
    })
    renderWithProviders(<HealthPage />, '/health')
    expect(await screen.findByText(/Everything looks healthy/)).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Warnings' })).toBeNull()
    expect(screen.getByText('read-only scope (gmail.readonly)')).toBeInTheDocument()
  })

  it('shows totals including tracked applications by stage', async () => {
    mockApi({ 'GET /api/health': () => healthy() })
    renderWithProviders(<HealthPage />, '/health')
    const totals = await screen.findByRole('region', { name: 'Totals' })
    expect(within(totals).getByText('17,728')).toBeInTheDocument()
    expect(within(totals).getByText('3 saved · 1 applied')).toBeInTheDocument()
  })

  it('shows an error state when the API is unreachable', async () => {
    mockApi({ 'GET /api/health': () => jsonResponse({ detail: 'database is locked' }, 500) })
    renderWithProviders(<HealthPage />, '/health')
    expect(await screen.findByRole('alert')).toHaveTextContent('database is locked')
  })
})

describe('matching section', () => {
  it('shows AI spend against the budget and embedding coverage', async () => {
    mockApi({
      'GET /api/health': () =>
        healthy({
          matching: {
            local_stages: [],
            projection: null,
            usage: { calls_today: 120, daily_call_cap: 200, usd_today: 0.41, usd_month: 1.42, monthly_usd_cap: 10, embedded_texts_today: 0, daily_embedding_cap: 20000 },
            embedding_coverage: 0.99,
            embedded: 8290,
            embeddable: 8364,
            scored_jobs: 130,
            last_relevance_run_at: '2026-10-12T09:00:00Z',
            last_llm_call_at: '2026-10-12T09:30:00Z',
            profiles: ['ai', 'fullstack'],
          },
        }),
    })
    renderWithProviders(<HealthPage />, '/health')
    const section = await screen.findByRole('region', { name: 'Matching' })
    expect(within(section).getByText('$1.42 of $10.00')).toBeInTheDocument()
    expect(within(section).getByText('120 of 200')).toBeInTheDocument()
    expect(within(section).getByText('99% of 100%')).toBeInTheDocument()
  })

  it('shows budget forecast line and warning when projected exceeds cap', async () => {
    mockApi({
      'GET /api/health': () =>
        healthy({
          matching: {
            local_stages: [],
            projection: null,
            budget_forecast: {
              month_to_date_usd: 4.5,
              avg_daily_usd_7d: 0.25,
              projected_month_usd: 8.5,
              cap_hit_date: '2026-10-18',
              warning: true,
              cap_usd: 5.0,
            },
            usage: { calls_today: 10, daily_call_cap: 60, usd_today: 0.2, usd_month: 4.5, monthly_usd_cap: 5, embedded_texts_today: 0, daily_embedding_cap: 20000 },
            embedding_coverage: 1.0,
            embedded: 100,
            embeddable: 100,
            scored_jobs: 50,
            last_relevance_run_at: '2026-10-12T09:00:00Z',
            last_llm_call_at: '2026-10-12T09:30:00Z',
            profiles: ['ai'],
          },
        }),
    })
    renderWithProviders(<HealthPage />, '/health')
    const forecast = await screen.findByTestId('budget-forecast')
    expect(within(forecast).getByText(/Budget forecast:/)).toBeInTheDocument()
    expect(within(forecast).getByText('$4.50')).toBeInTheDocument()
    expect(within(forecast).getByText('$0.25')).toBeInTheDocument()
    expect(within(forecast).getByText('$8.50')).toBeInTheDocument()
    expect(within(forecast).getByText('2026-10-18')).toBeInTheDocument()
    expect(screen.getByTestId('budget-forecast-warning')).toHaveTextContent('Warning: Projected spend exceeds monthly cap!')
  })
})
