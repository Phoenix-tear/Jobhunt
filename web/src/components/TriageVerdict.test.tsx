import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { Health, JobDetail, JobSummary } from '../api/types'
import { HealthPage } from '../pages/HealthPage'
import { JobsPage } from '../pages/JobsPage'
import { makeJob, mockApi, renderWithProviders } from '../test/utils'

const EMPTY_FACETS = {
  total_open: 0, levels: [], employment_types: [], countries: [], sources: [], remote: 0, needs_description: 0, new_24h: 0,
  generated_at: 'x', scored: 0, relevant: 0, filtered_out: 0, workplace_types: [], remote_open_to_india: 0, gate_rules: [],
  location_fits: [], show_other_locations_default: false,
}

const VERDICT = {
  model: 'gemini-3.8-flash', rubric_version: 'v1', created_at: '2026-10-24T01:00:00Z', level: 'senior', required_years_min: 6,
  suitable_for_new_grad: 'no', india_eligible: 'unclear', location_scope: 'unclear', eligibility_blockers: [], role_family: ['backend'],
  confidence: 'high', unverified_fields: [], used_by: 'llm_seniority', used_text: 'this verdict moved the job', would_exclude: true,
  human_label: null, human_reason: null,
  quotes: [
    { field: 'Level', value: 'senior', quote: 'Senior Engineer to own the platform' },
    { field: 'Years required', value: '6+', quote: '6+ years of backend experience' },
  ],
}

function detail(job: JobSummary, triage: unknown): JobDetail {
  return {
    ...job,
    match: {
      final_score: null, llm_score: null, reason: null, matched_skills: [], missing_skills: [], red_flags: [], recommended_profile: null,
      similarity_ai: null, similarity_fullstack: null, similarity_percentile: null, components: {}, gate_status: 'excluded',
      gate_rule: 'llm_seniority', exclusion_reason: 'LLM read the posting as level senior', required_years: null, remote_evidence: null,
      remote_regions: [], remote_source: null,
    },
    triage, description_text: 'Body', apply_url: 'https://example.test/apply', sightings: [], company: null, application: null,
    salary_min: null, salary_max: null, salary_currency: null, sponsorship_note: null, graduation_year_hint: null,
    last_seen_at: 'x', closed_at: null, signals: [], eligibility: [], eligibility_screens: [],
  } as unknown as JobDetail
}

function page(items: JobSummary[]) {
  return { total: items.length, page: 1, page_size: 25, sort: 'match', items }
}

describe('LLM verdict panel', () => {
  it('shows the verdict, the exact quotes and which rule used it', async () => {
    const job = makeJob({ id: 5, title: 'Backend Engineer' })
    mockApi({
      'GET /api/jobs': () => page([job]),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/jobs/5': () => detail(job, { verdict: VERDICT, student: { p_senior: 0.93, rules: [{ rule: 'title has "staff"', precision: 0.95 }], demoted_by: 'student_rule' } }),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=5')
    const panel = await screen.findByRole('complementary', { name: 'Job details' })
    const group = await within(panel).findByRole('group', { name: 'LLM verdict' })
    expect(within(group).getByTestId('verdict-summary')).toHaveTextContent('senior, asks 6+ years')
    const evidence = within(group).getByRole('list', { name: 'Verdict evidence' })
    expect(within(evidence).getByText(/Senior Engineer to own the platform/)).toBeInTheDocument()
    expect(within(evidence).getByText(/6\+ years of backend experience/)).toBeInTheDocument()
    expect(within(group).getByTestId('verdict-used')).toHaveTextContent('Excluded by this verdict')
    expect(within(group).getByTestId('student-chips')).toHaveTextContent('senior 93%')
    expect(within(group).getByTestId('student-chips')).toHaveTextContent('title has "staff"')
  })

  it('"LLM verdict is wrong" posts the label and the panel then offers to use the verdict again', async () => {
    const user = userEvent.setup()
    const job = makeJob({ id: 5, title: 'Backend Engineer' })
    let marked = false
    const api = mockApi({
      'GET /api/jobs': () => page([job]),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/jobs/5': () => detail(job, { verdict: { ...VERDICT, human_label: marked ? 'wrong_years' : null, used_by: marked ? '' : 'llm_seniority',
                                                         used_text: marked ? 'you marked this verdict wrong, so it is ignored' : 'x' }, student: null }),
      'POST /api/jobs/5/llm-verdict-wrong': () => { marked = true; return { job_id: 5, human_label: 'wrong_years' } },
      'DELETE /api/jobs/5/llm-verdict-wrong': () => { marked = false; return { job_id: 5, human_label: null } },
    })
    renderWithProviders(<JobsPage />, '/jobs?job=5')
    const group = await screen.findByRole('group', { name: 'LLM verdict' })
    await user.selectOptions(within(group).getByLabelText('What was wrong'), 'wrong_years')
    await user.click(within(group).getByRole('button', { name: 'LLM verdict is wrong' }))
    await waitFor(() => {
      expect(api.lastCall('POST', '/api/jobs/5/llm-verdict-wrong')?.body).toMatchObject({ label: 'wrong_years' })
    })
    const undo = await within(await screen.findByRole('group', { name: 'LLM verdict' })).findByRole('button', { name: 'Use the verdict again' })
    expect(screen.getByText(/You marked this verdict wrong \(wrong years\)/)).toBeInTheDocument()
    await user.click(undo)
    await waitFor(() => expect(api.lastCall('DELETE', '/api/jobs/5/llm-verdict-wrong')).toBeTruthy())
  })

  it('says so when no quote could be verified and lists dropped fields', async () => {
    const job = makeJob({ id: 5, title: 'Backend Engineer' })
    mockApi({
      'GET /api/jobs': () => page([job]),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/jobs/5': () => detail(job, { verdict: { ...VERDICT, quotes: [], level: 'unknown', required_years_min: null, would_exclude: false,
                                                         used_by: '', used_text: '', unverified_fields: ['level'] }, student: null }),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=5')
    const group = await screen.findByRole('group', { name: 'LLM verdict' })
    expect(within(group).getByText(/No quote could be verified/)).toBeInTheDocument()
    expect(within(group).getByText(/Dropped, quote not found in the posting: level/)).toBeInTheDocument()
  })
})

describe('Health: LLM triage', () => {
  it('shows verdict counts, leak rate, hallucinated-evidence rate, demotions and my agreement', async () => {
    const health = {
      generated_at: '2026-10-24T05:00:00Z', jobs_open: 10, jobs_closed: 1, companies_active: 1, companies_unverified: 0, companies_inactive: 0,
      applications_by_stage: {}, sources: [], matching: null, warnings: [], user_agent_contact_set: true, schema_version: 19,
      gmail: { credentials_file_exists: true, token_file_exists: true, scopes: [], read_only_scope: true, account_configured: null },
      triage: {
        enabled: true, rubric_version: 'v1', model: 'gemini-3.8-flash', verdicts: 120, by_level: { senior: 30, entry: 60, unknown: 30 },
        leak_rate_last_100: 0.03, judged_in_window: 100, leaked_in_window: 3, hallucinated_evidence_rate: 0.05, hallucinated_fields: 6,
        claimed_fields: 120, demoted_week_llm: 14, demoted_week_classifier: 4, demoted_week_rules: 9, verdict_wrong_marked: 2,
        verdicts_that_acted: 40, agreement: 0.95, daily_calls_cap: 20, calls_today: 7,
      },
    } as unknown as Health
    mockApi({ 'GET /api/health': () => health })
    renderWithProviders(<HealthPage />, '/health')
    const section = await screen.findByRole('region', { name: 'LLM triage' })
    expect(within(section).getByTestId('triage-leak')).toHaveTextContent('3.0%')
    expect(within(section).getByTestId('triage-hallucinated')).toHaveTextContent('5.0%')
    expect(within(section).getByTestId('triage-demoted')).toHaveTextContent('14 by the LLM')
    expect(within(section).getByTestId('triage-demoted')).toHaveTextContent('4 by the classifier')
    expect(within(section).getByTestId('triage-agreement')).toHaveTextContent('95.0% (2 of 40')
    expect(within(section).getByText(/Calls today/)).toHaveTextContent('7 of 20')
  })
})
