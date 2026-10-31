import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { LearningSection } from '../components/LearningSection'
import type { JobDetail, JobSummary } from '../api/types'
import { makeJob, mockApi, renderWithProviders } from '../test/utils'
import { JobsPage } from './JobsPage'

const EMPTY_FACETS = {
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

const RESUMES = {
  total: 2,
  counts: { visible: 2 },
  items: [
    { id: 9, code: 'R-0009', version: 3, lifecycle: 'draft', run_status: 'ok', job_id: 5 },
    { id: 7, code: 'R-0007', version: 2, lifecycle: 'approved', run_status: 'ok', job_id: 5 },
  ],
}

describe('Applied dialog', () => {
  it('defaults to the latest approved resume, posts the details and offers Undo', async () => {
    const user = userEvent.setup()
    const job = makeJob({ id: 5, title: 'AI Engineer', company_name: 'Collinear AI' })
    const api = mockApi({
      'GET /api/jobs': () => page([job]),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/jobs/5': () => detail(job),
      'GET /api/resumes': () => RESUMES,
      'POST /api/jobs/5/decision/applied': () => ({ job_id: 5, action: 'applied', application_id: 1 }),
      'POST /api/jobs/5/decision/undo': () => ({ job_id: 5, action: 'undo', reverted: 'applied' }),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=5')
    const panel = await screen.findByRole('complementary', { name: 'Job details' })
    await user.click(await within(panel).findByRole('button', { name: 'Applied' }))
    const dialog = await screen.findByRole('dialog', { name: /Applied: AI Engineer/ })
    const resume = (await within(dialog).findByLabelText('Resume version')) as HTMLSelectElement
    await waitFor(() => expect(resume.value).toBe('7'))                                    // latest approved, not the newest draft
    expect(within(dialog).getByRole('option', { name: 'None / other' })).toBeInTheDocument()
    await user.selectOptions(within(dialog).getByLabelText('How'), 'linkedin')
    await user.type(within(dialog).getByLabelText('Note (optional)'), 'via a referral')
    await user.click(within(dialog).getByRole('button', { name: 'Confirm applied' }))
    await waitFor(() => {
      const body = api.lastCall('POST', '/api/jobs/5/decision/applied')?.body as Record<string, unknown>
      expect(body).toMatchObject({ how: 'linkedin', resume_id: 7, note: 'via a referral' })
      expect(typeof body.applied_at).toBe('string')
    })
    await user.click(await screen.findByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(api.callsTo('POST', '/api/jobs/5/decision/undo')).toHaveLength(1))
  })
})

describe('triage on the list', () => {
  const jobs = [makeJob({ id: 1, title: 'First Role' }), makeJob({ id: 2, title: 'Second Role' })]
  const base = () => ({
    'GET /api/jobs': () => page(jobs),
    'GET /api/facets': () => EMPTY_FACETS,
    'GET /api/jobs/1': () => detail(jobs[0]!),
    'GET /api/jobs/2': () => detail(jobs[1]!),
    'GET /api/resumes': () => RESUMES,
    'POST /api/jobs/1/decision/save': () => ({ job_id: 1, action: 'saved' }),
  })

  it('keys: j/k move, s saves, x and a open dialogs, ? lists the shortcuts', async () => {
    const api = mockApi(base())
    renderWithProviders(<JobsPage />, '/jobs?job=1')
    await screen.findByRole('complementary', { name: 'Job details' })
    await screen.findByText('Second Role')                                                   // the list has loaded
    fireEvent.keyDown(window, { key: 'j' })
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('job=2'))
    fireEvent.keyDown(window, { key: 'k' })
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('job=1'))
    fireEvent.keyDown(window, { key: 's' })
    await waitFor(() => expect(api.callsTo('POST', '/api/jobs/1/decision/save')).toHaveLength(1))
    fireEvent.keyDown(window, { key: 'x' })
    expect(await screen.findByRole('dialog', { name: /Not applying: First Role/ })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /Not applying/ })).toBeNull())
    fireEvent.keyDown(window, { key: 'a' })
    expect(await screen.findByRole('dialog', { name: /Applied: First Role/ })).toBeInTheDocument()
  })

  it('no key tailors a resume, and shortcuts are ignored while typing', async () => {
    const api = mockApi(base())
    renderWithProviders(<JobsPage />, '/jobs?job=1')
    await screen.findByRole('complementary', { name: 'Job details' })
    await screen.findByText('Second Role')
    for (const key of ['t', 'T', 'r', 'R', 'e', 'w', 'Enter', ' ']) fireEvent.keyDown(window, { key })
    expect(api.calls.some((call) => call.url.pathname.endsWith('/tailor'))).toBe(false)
    expect(api.calls.filter((call) => call.method !== 'GET')).toEqual([])
    fireEvent.keyDown(screen.getByRole('searchbox'), { key: 's' })                       // typing in the search box
    expect(api.callsTo('POST', '/api/jobs/1/decision/save')).toHaveLength(0)
    fireEvent.keyDown(window, { key: '?' })
    const help = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
    expect(within(help).getByText(/no shortcut on purpose/i)).toBeInTheDocument()
  })

  it('bulk: tick two rows, choose one reason, one request', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      ...base(),
      'POST /api/decisions/not-applying': () => [{ job_id: 1, action: 'not_applying' }, { job_id: 2, action: 'not_applying' }],
    })
    renderWithProviders(<JobsPage />, '/jobs')
    await user.click(await screen.findByRole('checkbox', { name: 'Select First Role' }))
    await user.click(screen.getByRole('checkbox', { name: 'Select Second Role' }))
    const bulk = screen.getByRole('region', { name: 'Bulk actions' })
    expect(bulk).toHaveTextContent('2 selected')
    await user.click(within(bulk).getByRole('button', { name: 'Not applying…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Not applying to 2 jobs' })
    await user.click(within(dialog).getByRole('radio', { name: 'Wrong location' }))
    await user.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(api.lastCall('POST', '/api/decisions/not-applying')?.body).toEqual({ job_ids: [1, 2], reason: 'wrong_location' }))
  })

  it('tabs switch the view in the URL and in the request', async () => {
    const user = userEvent.setup()
    const api = mockApi(base())
    renderWithProviders(<JobsPage />, '/jobs')
    await screen.findByText('First Role')
    const tabs = screen.getByRole('navigation', { name: 'View' })
    expect(within(tabs).getByRole('button', { name: 'Open' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(within(tabs).getByRole('button', { name: 'Not applying' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('view=not_applying'))
    await waitFor(() => expect(api.calls.filter((c) => c.url.pathname === '/api/jobs').at(-1)?.url.searchParams.get('view')).toBe('not_applying'))
    for (const name of ['Saved', 'Applied', 'All']) expect(within(tabs).getByRole('button', { name })).toBeInTheDocument()
  })
})

describe('eligibility and why this match in the job panel', () => {
  it('shows what the posting says, both similarities, the formula and the company warning', async () => {
    const job = makeJob({ id: 5, title: 'Research Scientist Intern', company_name: 'Collinear AI',
      likely_ineligible: 'Likely not eligible: PhD required', applied_company_warning: 'You applied to Collinear AI on 17 Oct (R-0007)' })
    mockApi({
      'GET /api/jobs': () => page([job]),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/resumes': () => ({ total: 0, items: [], counts: {} }),
      'GET /api/jobs/5/tailor-estimate': () => ({ job_id: 5, model: 'm', estimated_usd: 0, interactive_calls_today: 0, interactive_call_cap: 40, blocked: false, message: '', existing_versions: 0, will_use_model: false }),
      'GET /api/jobs/5': () => detail(job, {
        match: { final_score: 50, llm_score: null, reason: null, matched_skills: [], missing_skills: [], red_flags: [], recommended_profile: 'fullstack',
          similarity_ai: 0.7006, similarity_fullstack: 0.7037, similarity_percentile: 0.95, components: { fit: 87.39 }, gate_status: 'maybe',
          gate_rule: 'likely_ineligible', exclusion_reason: null, required_years: null, remote_evidence: null, remote_regions: [], remote_source: null },
        eligibility: [{ kind: 'phd_required', strength: 'required', label: 'PhD required', snippet: 'must be currently pursuing a PhD' }],
        why_match: { fit_reason: null, fit: 87.39, fit_source: 'ranker', fit_source_note: 'estimated by your local ranker, no Gemini score yet',
          similarity_ai: 0.7006, similarity_fullstack: 0.7037, best_profile: 'fullstack', profile_note: 'Full-stack profile won on similarity (0.704 vs 0.701, difference 0.003); that is a near tie',
          formula: 'score = 55% fit + 25% similarity rank + 20% location', components: {} },
        learned_rules: [{ id: 'L-0001', phrase: 'research scientist', tooltip: 'Learned rule L-0001' }],
      }),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=5')
    const panel = await screen.findByRole('complementary', { name: 'Job details' })
    const why = await within(panel).findByRole('region', { name: 'Why this match' })
    expect(within(panel).getAllByText('Likely not eligible: PhD required').length).toBeGreaterThan(0)
    expect(within(panel).getByText('You applied to Collinear AI on 17 Oct (R-0007)')).toBeInTheDocument()
    expect(within(why).getByText(/posting says: “must be currently pursuing a PhD”/)).toBeInTheDocument()
    expect(within(why).getByTestId('profile-similarity')).toHaveTextContent('AI/ML 0.701')
    expect(within(why).getByTestId('profile-similarity')).toHaveTextContent('Full-stack 0.704')
    expect(within(why).getByText(/near tie/)).toBeInTheDocument()
    expect(within(why).getByText(/No Gemini reason stored/)).toBeInTheDocument()
    expect(within(why).getByText(/estimated by your local ranker/)).toBeInTheDocument()
    expect(within(why).getByTestId('score-formula')).toHaveTextContent('55% fit + 25% similarity rank + 20% location')
    expect(within(panel).getAllByTitle('Learned rule L-0001').length).toBeGreaterThan(0)
  })
})

describe('learning section', () => {
  it('is honest about how few decisions exist, and approves / removes rules only on click', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      'GET /api/learning': () => ({
        labels: { applied: 1, saved: 2, not_applying_role_fit: 3, not_applying_other_reasons: 4, older_like_dismiss_clicks: 0 },
        labels_in_last_training: {}, human_labels: 6,
        ranker: { trained_at: '2026-10-19T00:00:00Z', rows: 257, chosen: 'ridge_numeric', cv_spearman: 0.464, similarity_only_spearman: 0.316,
          in_use: 'previous', version: 'v', last_decision: { action: 'kept_previous', reason: 'new CV Spearman is lower', rows: 280, at: 'x' } },
        proposals: [{ key: 'title_phrase:quantum', kind: 'title_phrase', phrase: 'quantum', count: 6, job_ids: [1, 2, 3, 4, 5, 6], kept: 0,
          reason: 'wrong_role x6', precision: 1, examples: ['Quantum Researcher 1'] }],
        rules: [{ id: 'L-0001', kind: 'title_phrase', phrase: 'blockchain', demote: true, penalty: 8, count: 5, job_ids: [1], created: '2026-10-19', reason: 'x' }],
        honesty: 'Only decisions you made yourself teach the ranker what you like. It improves only as you label more.',
        applies: 'Approved or removed rules take effect at the next relevance run.',
      }),
      'POST /api/learning/proposals/approve': () => ({ id: 'L-0002' }),
      'DELETE /api/learning/rules/L-0001': () => ({ id: 'L-0001' }),
    })
    renderWithProviders(<LearningSection />, '/health')
    expect(await screen.findByTestId('learning-honesty')).toHaveTextContent('6 role-fit decisions')
    expect(screen.getByTestId('learning-honesty')).toHaveTextContent('improves only as you label more')
    expect(screen.getByTestId('ranker-state')).toHaveTextContent('model in use: previous')
    expect(screen.getByTestId('ranker-state')).toHaveTextContent('kept previous')
    expect(api.callsTo('POST', '/api/learning/proposals/approve')).toHaveLength(0)         // nothing is applied by itself
    const suggestions = screen.getByRole('list', { name: 'Suggested rules' })
    expect(within(suggestions).getByText(/6 of your decisions/)).toBeInTheDocument()
    await user.click(within(suggestions).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(api.lastCall('POST', '/api/learning/proposals/approve')?.body).toEqual({ key: 'title_phrase:quantum' }))
    await user.click(within(screen.getByRole('list', { name: 'Active learned rules' })).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(api.callsTo('DELETE', '/api/learning/rules/L-0001')).toHaveLength(1))
  })
})
