import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Health, JobDetail, JobSummary } from '../api/types'
import { jsonResponse, makeJob, mockApi, renderWithProviders } from '../test/utils'
import { HealthPage } from './HealthPage'
import { JobsPage } from './JobsPage'

afterEach(() => {
  vi.unstubAllGlobals()
})

const FACETS = {
  total_open: 0, levels: [], employment_types: [], countries: [], sources: [], remote: 0, needs_description: 0, new_24h: 0,
  generated_at: 'x', scored: 0, relevant: 0, filtered_out: 0, workplace_types: [], remote_open_to_india: 0, gate_rules: [],
  location_fits: [], show_other_locations_default: false,
}

function page(items: JobSummary[]) {
  return { total: items.length, page: 1, page_size: 25, sort: 'match', items }
}

function detail(job: JobSummary, extra: Record<string, unknown> = {}): JobDetail {
  return {
    ...job, match: null, description_text: 'Body', apply_url: 'https://example.test/apply', sightings: [], company: null, application: null,
    salary_min: null, salary_max: null, salary_currency: null, sponsorship_note: null, graduation_year_hint: null,
    last_seen_at: 'x', closed_at: null, signals: [], eligibility: [], eligibility_screens: [], ...extra,
  } as unknown as JobDetail
}

const PREVIEW_TEXT = '\n- company: "Barclays"\n  deny_title_patterns: ["Vice President"]\n  note: "Added from Not applying (ineligible_level) on 2026-10-21"\n'

describe('Not applying: Ineligible grade / level and "Remember for this company"', () => {
  const job = makeJob({ id: 5, title: 'Vice President, Technology', company_name: 'Barclays' })
  const routes = () => ({
    'GET /api/jobs': () => page([job]),
    'GET /api/facets': () => FACETS,
    'GET /api/jobs/5': () => detail(job),
    'POST /api/jobs/5/decision/not-applying': () => ({ job_id: 5, action: 'not_applying', reason: 'ineligible_level' }),
    'POST /api/company-rules/preview': () => ({ company: 'Barclays', pattern: 'Vice President', text: PREVIEW_TEXT, path: 'data/profile/company_rules.yaml', already_denied: false }),
    'POST /api/company-rules': () => ({ path: 'data/profile/company_rules.yaml', rules: [] }),
    'GET /api/company-rules': () => ({ path: 'data/profile/company_rules.yaml', rules: [] }),
  })

  async function openDialog(user: ReturnType<typeof userEvent.setup>) {
    renderWithProviders(<JobsPage />, '/jobs?job=5')
    const panel = await screen.findByRole('complementary', { name: 'Job details' })
    await user.click(await within(panel).findByRole('button', { name: 'Not applying' }))
    const dialog = await screen.findByRole('dialog', { name: /Not applying: Vice President/ })
    await user.click(within(dialog).getByRole('radio', { name: /Ineligible: grade \/ level/ }))
    return dialog
  }

  it('shows the exact line first and writes it only after the confirming click', async () => {
    const user = userEvent.setup()
    const api = mockApi(routes())
    const dialog = await openDialog(user)
    await user.click(within(dialog).getByRole('checkbox', { name: /remember for this company/i }))
    const preview = await within(dialog).findByTestId('rule-preview')
    expect(preview).toHaveTextContent('company: "Barclays"')
    expect(preview).toHaveTextContent('deny_title_patterns: ["Vice President"]')
    expect(api.callsTo('POST', '/api/company-rules')).toHaveLength(0) // only a preview so far
    expect(api.callsTo('POST', '/api/jobs/5/decision/not-applying')).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Confirm and write this line' }))
    await waitFor(() => expect(api.callsTo('POST', '/api/company-rules')).toHaveLength(1))
    expect(api.lastCall('POST', '/api/jobs/5/decision/not-applying')?.body).toMatchObject({ reason: 'ineligible_level' })
    expect(api.lastCall('POST', '/api/company-rules')?.body).toEqual({ job_id: 5, pattern: 'Vice President', text: PREVIEW_TEXT, confirmed: true })
  })

  it('without the checkbox only the decision is saved and no rule is written', async () => {
    const user = userEvent.setup()
    const api = mockApi(routes())
    const dialog = await openDialog(user)
    await user.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(api.callsTo('POST', '/api/jobs/5/decision/not-applying')).toHaveLength(1))
    expect(api.callsTo('POST', '/api/company-rules/preview')).toHaveLength(0)
    expect(api.callsTo('POST', '/api/company-rules')).toHaveLength(0)
  })

  it('unticking the box after the preview writes nothing', async () => {
    const user = userEvent.setup()
    const api = mockApi(routes())
    const dialog = await openDialog(user)
    const box = within(dialog).getByRole('checkbox', { name: /remember for this company/i })
    await user.click(box)
    await within(dialog).findByTestId('rule-preview')
    await user.click(box)
    await user.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(api.callsTo('POST', '/api/jobs/5/decision/not-applying')).toHaveLength(1))
    expect(api.callsTo('POST', '/api/company-rules')).toHaveLength(0)
  })

  it('says so when the title has no level word and still saves the decision', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      ...routes(),
      'POST /api/company-rules/preview': () => jsonResponse({ detail: 'no level word (Vice President, Director, ...) in this title' }, 422),
    })
    const dialog = await openDialog(user)
    await user.click(within(dialog).getByRole('checkbox', { name: /remember for this company/i }))
    expect(await within(dialog).findByText(/Nothing to remember/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(api.callsTo('POST', '/api/jobs/5/decision/not-applying')).toHaveLength(1))
    expect(api.callsTo('POST', '/api/company-rules')).toHaveLength(0)
  })

  it('offers the closed / too old reasons', async () => {
    const user = userEvent.setup()
    mockApi(routes())
    const dialog = await openDialog(user)
    expect(within(dialog).getByRole('radio', { name: /Closed or no longer available/ })).toBeInTheDocument()
    expect(within(dialog).getByRole('radio', { name: /Too old/ })).toBeInTheDocument()
    expect(within(dialog).queryByRole('checkbox', { name: /remember for this company/i })).toBeInTheDocument() // still the level reason
    await user.click(within(dialog).getByRole('radio', { name: /Too old/ }))
    expect(within(dialog).queryByRole('checkbox', { name: /remember for this company/i })).not.toBeInTheDocument()
  })
})

describe('job panel: Check if still open', () => {
  const job = makeJob({ id: 6, title: 'Backend Engineer', auto_hidden: 'closed_checked', freshness: 'aging', age_days: 40 })

  it('requests nothing until the click, then shows the answer and when it was checked', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      'GET /api/jobs': () => page([job]),
      'GET /api/facets': () => FACETS,
      'GET /api/jobs/6': () => detail(job),
      'POST /api/jobs/6/check-open': () => ({ job_id: 6, result: 'closed', detail: 'HTTP 404 from the job API', http_status: 404, method: 'ats_api', checked_at: new Date().toISOString(), action: 'closed' }),
      'POST /api/jobs/6/restore': () => ({ job_id: 6, restored: true }),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=6')
    const panel = await screen.findByRole('complementary', { name: 'Job details' })
    await within(panel).findByText(/Not checked yet/)
    expect(api.callsTo('POST', '/api/jobs/6/check-open')).toHaveLength(0)
    await user.click(within(panel).getByRole('button', { name: 'Check if still open' }))
    const result = await within(panel).findByTestId('liveness-result')
    expect(result).toHaveTextContent('Closed: no longer available')
    expect(result).toHaveTextContent('HTTP 404 from the job API')
    expect(result).toHaveTextContent(/checked \d+m ago/)
    await user.click(within(panel).getByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(api.callsTo('POST', '/api/jobs/6/restore')).toHaveLength(1))
  })

  it('shows an uncertain answer without claiming anything changed', async () => {
    const plain = makeJob({ id: 6, title: 'Backend Engineer' })
    mockApi({
      'GET /api/jobs': () => page([plain]),
      'GET /api/facets': () => FACETS,
      'GET /api/jobs/6': () => detail(plain, { liveness: { result: 'uncertain', detail: 'HTTP 429', http_status: 429, method: 'page', checked_at: new Date().toISOString() } }),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=6')
    const panel = await screen.findByRole('complementary', { name: 'Job details' })
    expect(await within(panel).findByTestId('liveness-result')).toHaveTextContent(/Could not tell.*nothing was changed/)
    expect(within(panel).queryByRole('button', { name: 'Restore' })).not.toBeInTheDocument()
  })
})

describe('Health: freshness and company rules', () => {
  const health = (): Health =>
    ({
      matching: null, generated_at: '2026-10-21T10:00:00Z', jobs_open: 10, jobs_closed: 1, companies_active: 1, companies_unverified: 0, companies_inactive: 0,
      applications_by_stage: {}, sources: [], gmail: { credentials_file_exists: false, token_file_exists: false, scopes: [], read_only_scope: false, account_configured: null },
      user_agent_contact_set: true, schema_version: 17, warnings: [],
      freshness: {
        enabled: true, fresh_days: 30, stale_after_days: 45, auto_hidden_total: 5, last_check: null,
        auto_hidden_by_source: [{ value: 'greenhouse', count: 4 }, { value: 'lever', count: 1 }],
        auto_hidden_by_reason: [{ value: 'auto_stale', count: 5 }],
        bands: [{ value: 'fresh', count: 3 }, { value: 'unknown', count: 2 }],
        source_reports: [
          { source: 'arbeitnow', shown: 5, reported_closed: 6, reported_too_old: 6, decided: 12, closed_share: 1, auto_hidden: 0, current_stale_after_days: 45,
            proposed_stale_after_days: 30, proposal: '12 of 12 jobs you looked at from arbeitnow were closed or too old: consider freshness.source_stale_after_days: {arbeitnow: 30} in config.yaml (not applied)' },
          { source: 'lever', shown: 1, reported_closed: 0, reported_too_old: 0, decided: 0, closed_share: 0, auto_hidden: 1, current_stale_after_days: 45, proposed_stale_after_days: null, proposal: null },
        ],
      },
    }) as unknown as Health

  const BARCLAYS = {
    index: 0, entry_indexes: [0], company: 'Barclays', aliases: [], allow_title_patterns: ['Analyst', 'Technology Developer'],
    deny_title_patterns: ['Assistant Vice President', 'Vice President', 'AVP', 'VP', 'Director'], allow_level_codes: ['BA4'], deny_level_codes: ['BA3', 'BA2', 'BA1'],
    deny_text_patterns: [], note: 'Only the BA-4 grade is open to me (user rule)',
  }

  it('shows auto-hidden counts per source and the report with its proposal (never applied)', async () => {
    mockApi({ 'GET /api/health': () => health(), 'GET /api/company-rules': () => ({ path: 'p', rules: [] }), 'GET /api/learning': () => jsonResponse({ detail: 'x' }, 500) })
    renderWithProviders(<HealthPage />, '/health')
    const section = await screen.findByRole('region', { name: 'Freshness' })
    expect(within(section).getByTestId('auto-hidden-line')).toHaveTextContent(/5 auto-hidden by source:.*Greenhouse 4.*Lever 1/)
    const row = within(section).getByRole('row', { name: /arbeitnow/i })
    expect(row).toHaveTextContent('6')
    expect(within(section).getByRole('list', { name: 'Proposals' })).toHaveTextContent('not applied')
  })

  it('lists company rules and removes one only after a second click', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      'GET /api/health': () => health(),
      'GET /api/company-rules': () => ({ path: 'data/profile/company_rules.yaml', rules: [BARCLAYS] }),
      'DELETE /api/company-rules/0': () => ({ path: 'data/profile/company_rules.yaml', rules: [] }),
      'GET /api/learning': () => jsonResponse({ detail: 'x' }, 500),
    })
    renderWithProviders(<HealthPage />, '/health')
    const section = await screen.findByRole('region', { name: 'Company rules' })
    const rule = await within(section).findByText('Barclays')
    expect(rule).toBeInTheDocument()
    expect(within(section).getByText(/Only the BA-4 grade is open to me/)).toBeInTheDocument()
    expect(within(section).getByText(/BA3, BA2, BA1/)).toBeInTheDocument()
    await user.click(within(section).getByRole('button', { name: 'Remove' }))
    expect(api.callsTo('DELETE', '/api/company-rules/0')).toHaveLength(0)
    await user.click(within(section).getByRole('button', { name: 'Keep' }))
    expect(api.callsTo('DELETE', '/api/company-rules/0')).toHaveLength(0)
    await user.click(within(section).getByRole('button', { name: 'Remove' }))
    await user.click(within(section).getByRole('button', { name: 'Yes, remove' }))
    await waitFor(() => expect(api.callsTo('DELETE', '/api/company-rules/0')).toHaveLength(1))
  })
})
