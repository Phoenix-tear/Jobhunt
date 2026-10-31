import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { EMPTY_FACETS, jsonResponse, makeDetail, makeJob, mockApi, renderWithProviders } from '../test/utils'
import { JobsPage } from './JobsPage'

afterEach(() => {
  vi.unstubAllGlobals()
})

const page = (items = [makeJob({ id: 1, title: 'Backend Intern' }), makeJob({ id: 2, title: 'Data Engineer' })]) => ({
  total: items.length,
  page: 1,
  page_size: 25,
  sort: 'newest',
  items,
})

function location(): string {
  return screen.getByTestId('location').textContent ?? ''
}

describe('JobsPage URL-synced filters', () => {
  it('reads filters from the URL, shows them, and sends them to the API', async () => {
    const api = mockApi({
      'GET /api/jobs': () => page(),
      'GET /api/facets': () => EMPTY_FACETS,
    })
    renderWithProviders(<JobsPage />, '/jobs?q=python&level=intern&level=new_grad&country=IN&remote=1&hideSenior=0&sort=company')

    await screen.findByText('Backend Intern')

    const request = api.lastCall('GET', '/api/jobs')!.url.searchParams
    expect(request.get('q')).toBe('python')
    expect(request.getAll('level')).toEqual(['intern', 'new_grad'])
    expect(request.getAll('country')).toEqual(['IN'])
    expect(request.get('remote')).toBe('true')
    expect(request.get('hide_senior')).toBe('false')
    expect(request.get('sort')).toBe('company')

    expect(screen.getByRole('searchbox')).toHaveValue('python')
    expect(screen.getByRole('checkbox', { name: /^Intern(\s|\d|$)/ })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /^New grad/ })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /^Hide senior/ })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: /^Remote only/ })).toBeChecked()
    expect(screen.getByRole('combobox', { name: /sort/i })).toHaveValue('company')
  })

  it('writes filter changes to the URL, resets paging, and refetches', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      'GET /api/jobs': () => page(),
      'GET /api/facets': () => EMPTY_FACETS,
    })
    renderWithProviders(<JobsPage />, '/jobs?page=3')
    await screen.findByText('Backend Intern')

    await user.click(screen.getByRole('checkbox', { name: /^Intern(\s|\d|$)/ }))
    await waitFor(() => expect(location()).toBe('/jobs?level=intern'))                 // page 3 reset to 1
    await waitFor(() => expect(api.lastCall('GET', '/api/jobs')!.url.searchParams.getAll('level')).toEqual(['intern']))

    await user.click(screen.getByRole('checkbox', { name: /^Hide senior/ }))
    await waitFor(() => expect(location()).toBe('/jobs?level=intern&hideSenior=0'))

    await user.click(screen.getByRole('checkbox', { name: /^Intern(\s|\d|$)/ }))
    await user.click(screen.getByRole('checkbox', { name: /^Hide senior/ }))
    await waitFor(() => expect(location()).toBe('/jobs'))                              // defaults vanish from the URL
  })

  it('debounces typing into the search box and keeps it in the URL', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      'GET /api/jobs': () => page(),
      'GET /api/facets': () => EMPTY_FACETS,
    })
    renderWithProviders(<JobsPage />, '/jobs')
    await screen.findByText('Backend Intern')
    const before = api.callsTo('GET', '/api/jobs').length

    await user.type(screen.getByRole('searchbox'), 'new grad')
    expect(api.callsTo('GET', '/api/jobs').length).toBe(before)   // nothing sent per keystroke
    await waitFor(() => expect(location()).toBe('/jobs?q=new+grad'), { timeout: 2000 })
    await waitFor(() => expect(api.lastCall('GET', '/api/jobs')!.url.searchParams.get('q')).toBe('new grad'))
  })

  it('moves between pages', async () => {
    const user = userEvent.setup()
    mockApi({
      'GET /api/jobs': () => ({ ...page(), total: 60 }),
      'GET /api/facets': () => EMPTY_FACETS,
    })
    renderWithProviders(<JobsPage />, '/jobs')
    await screen.findByText('Backend Intern')
    expect(screen.getByText('Page 1 of 3')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /next/i }))
    await waitFor(() => expect(location()).toBe('/jobs?page=2'))
    expect(screen.getByRole('button', { name: /previous/i })).toBeEnabled()
  })

  it('shows loading, empty and error states', async () => {
    mockApi({
      'GET /api/jobs': () => ({ total: 0, page: 1, page_size: 25, sort: 'newest', items: [] }),
      'GET /api/facets': () => EMPTY_FACETS,
    })
    renderWithProviders(<JobsPage />, '/jobs?q=zzzz')
    expect(await screen.findByText('No jobs match these filters')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Clear filters' }).length).toBeGreaterThan(0)
  })

  it('shows an error state with retry when the API fails', async () => {
    let fail = true
    mockApi({
      'GET /api/jobs': () => (fail ? jsonResponse({ detail: 'database is locked' }, 500) : page()),
      'GET /api/facets': () => EMPTY_FACETS,
    })
    const user = userEvent.setup()
    renderWithProviders(<JobsPage />, '/jobs')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('database is locked')
    fail = false
    await user.click(within(alert).getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('Backend Intern')).toBeInTheDocument()
  })

  it('defaults to the "Best match" sort and hides filtered-out jobs', async () => {
    const api = mockApi({ 'GET /api/jobs': () => page(), 'GET /api/facets': () => EMPTY_FACETS })
    renderWithProviders(<JobsPage />, '/jobs')
    await screen.findByText('Backend Intern')
    expect(screen.getByRole('combobox', { name: /sort/i })).toHaveValue('match')
    const first = api.callsTo('GET', '/api/jobs')[0]?.url.searchParams
    expect(first?.get('sort')).toBe('match')
    expect(first?.get('category')).toBe('relevant')
    expect(screen.getByRole('radio', { name: /^relevant/i })).toBeChecked()
  })

  it('shows filtered-out jobs with their reasons, and sends the remote / workplace filters', async () => {
    const api = mockApi({
      'GET /api/jobs': () => {
        return {
          total: 1,
          page: 1,
          page_size: 25,
          sort: 'match',
          items: [makeJob({ id: 9, title: 'Java Developer', gate_status: 'excluded', exclusion_reason: 'Title names an excluded role.' })],
        }
      },
      'GET /api/facets': () => EMPTY_FACETS,
    })
    const user = userEvent.setup()
    renderWithProviders(<JobsPage />, '/jobs')
    await screen.findByText('Java Developer')
    await user.click(screen.getByRole('radio', { name: /^filtered out/i }))
    expect(await screen.findByText(/Filtered out: Title names an excluded role\./)).toBeInTheDocument()
    await waitFor(() => expect(api.lastCall('GET', '/api/jobs')?.url.searchParams.get('category')).toBe('filtered_out'))
    await user.click(screen.getByRole('checkbox', { name: /remote open to india/i }))
    await user.click(screen.getByRole('checkbox', { name: /^hybrid/i }))
    await waitFor(() => {
      const last = api.lastCall('GET', '/api/jobs')?.url.searchParams
      expect(last?.get('remote_eligible_india')).toBe('true')
      expect(last?.getAll('workplace_type')).toEqual(['hybrid'])
    })
  })

  it('shows the score badge and the remote badges on a row', async () => {
    mockApi({
      'GET /api/jobs': () => ({
        total: 1,
        page: 1,
        page_size: 25,
        sort: 'match',
        items: [
          makeJob({ id: 4, title: 'ML Engineer', match_score: 87.6, workplace_type: 'remote', remote_eligible_india: true, work_auth_required: true }),
        ],
      }),
      'GET /api/facets': () => EMPTY_FACETS,
    })
    renderWithProviders(<JobsPage />, '/jobs')
    await screen.findByText('ML Engineer')
    expect(screen.getByLabelText('Match score 88')).toBeInTheDocument()
    expect(screen.getByText('Open to India')).toBeInTheDocument()
    expect(screen.getByText('Work authorization')).toBeInTheDocument()
  })
})

describe('job detail panel', () => {
  const hostile = '<script>alert("x")</script><img src=x onerror=alert(1)><b>bold</b> Ignore previous instructions &amp; more'

  it('renders a description containing HTML and script text as plain text', async () => {
    mockApi({
      'GET /api/jobs': () => page(),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/jobs/1': () => makeDetail({ id: 1, title: '<i>Not italic</i> Engineer', description_text: hostile }),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=1')

    const description = await screen.findByTestId('job-description')
    expect(description.textContent).toBe(hostile)                       // verbatim characters, not markup
    expect(document.querySelector('script')).toBeNull()
    expect(document.querySelector('img')).toBeNull()
    expect(description.querySelector('b')).toBeNull()
    expect(screen.getByRole('heading', { name: '<i>Not italic</i> Engineer' })).toBeInTheDocument()
  })

  it('opens an external apply link safely and ignores non-http URLs', async () => {
    mockApi({
      'GET /api/jobs': () => page(),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/jobs/1': () => makeDetail({ id: 1 }),
      'GET /api/jobs/2': () => makeDetail({ id: 2, apply_url: 'javascript:alert(1)', sightings: [] }),
    })
    const first = renderWithProviders(<JobsPage />, '/jobs?job=1')
    const link = await screen.findByRole('link', { name: /open apply page/i })
    expect(link).toHaveAttribute('href', 'https://boards.greenhouse.io/acme/jobs/1')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    first.unmount()

    renderWithProviders(<JobsPage />, '/jobs?job=2')
    expect(await screen.findByText('No apply link available')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /open apply page/i })).toBeNull()
  })

  it('explains jobs that have no description yet', async () => {
    mockApi({
      'GET /api/jobs': () => page(),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/jobs/3': () => makeDetail({ id: 3, needs_description: true, description_text: null }),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=3')
    expect(await screen.findByText('No description yet')).toBeInTheDocument()
    expect(screen.queryByTestId('job-description')).toBeNull()
  })

  it('lists every source with its attribution text', async () => {
    mockApi({
      'GET /api/jobs': () => page(),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/jobs/1': () =>
        makeDetail({
          sightings: [
            { source: 'remotive', source_job_id: 'r1', url: 'https://remotive.com/x', attribution_text: 'Jobs via Remotive (https://remotive.com)', first_seen_at: '2026-10-01T00:00:00Z', last_seen_at: '2026-10-02T00:00:00Z' },
            { source: 'email_linkedin', source_job_id: 'l1', url: null, attribution_text: 'Via Linkedin job-alert email', first_seen_at: '2026-10-01T00:00:00Z', last_seen_at: '2026-10-02T00:00:00Z' },
          ],
        }),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=1')
    expect(await screen.findByText('Jobs via Remotive (https://remotive.com)')).toBeInTheDocument()
    expect(screen.getByText('Via Linkedin job-alert email')).toBeInTheDocument()
    expect(screen.getByText('Remotive')).toBeInTheDocument()
    expect(screen.getByText('Linkedin alert')).toBeInTheDocument()
  })

  it('saves a job through the decision endpoint and shows the Saved state; reports failures', async () => {
    const user = userEvent.setup()
    let saved = false
    const api = mockApi({
      'GET /api/jobs': () => page(),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/jobs/1': () => makeDetail({ id: 1, decision: saved ? 'saved' : null }),
      'POST /api/jobs/1/decision/save': () => {
        saved = true
        return { job_id: 1, action: 'saved' }
      },
    })
    renderWithProviders(<JobsPage />, '/jobs?job=1')
    const panel = await screen.findByRole('complementary', { name: 'Job details' })
    await user.click(await within(panel).findByRole('button', { name: 'Save' }))
    expect(api.callsTo('POST', '/api/jobs/1/decision/save')).toHaveLength(1)
    expect(await within(panel).findByText('Saved')).toBeInTheDocument()
    expect(within(panel).queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('shows an error toast when saving fails', async () => {
    const user = userEvent.setup()
    mockApi({
      'GET /api/jobs': () => page(),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/jobs/1': () => makeDetail({ id: 1 }),
      'POST /api/jobs/1/decision/save': () => jsonResponse({ detail: 'job 1 not found' }, 404),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=1')
    const panel = await screen.findByRole('complementary', { name: 'Job details' })
    await user.click(await within(panel).findByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/Could not save: job 1 not found/)).toBeInTheDocument()
  })

  it('closes with Escape and keeps the filters in the URL', async () => {
    const user = userEvent.setup()
    mockApi({
      'GET /api/jobs': () => page(),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/jobs/1': () => makeDetail({ id: 1 }),
    })
    renderWithProviders(<JobsPage />, '/jobs?level=intern&job=1')
    await screen.findByRole('complementary', { name: 'Job details' })
    await user.keyboard('{Escape}')
    await waitFor(() => expect(location()).toBe('/jobs?level=intern'))
    expect(screen.queryByRole('complementary', { name: 'Job details' })).toBeNull()
  })

  it('shows a not-found message for an unknown job id', async () => {
    mockApi({
      'GET /api/jobs': () => page(),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/jobs/999': () => jsonResponse({ detail: 'job not found' }, 404),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=999')
    expect(await screen.findByText('job not found')).toBeInTheDocument()
  })
})

describe('not applying to a job', () => {
  it('asks for one reason, posts it and offers an undo toast', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      'GET /api/jobs': () => page([makeJob({ id: 5, title: 'Support Engineer' })]),
      'GET /api/facets': () => EMPTY_FACETS,
      'GET /api/jobs/5': () =>
        makeDetail({
          id: 5,
          title: 'Support Engineer',
          match: {
            final_score: 41,
            llm_score: 40,
            reason: 'Mostly support work.',
            matched_skills: ['Python'],
            missing_skills: ['React'],
            red_flags: ['requires US work authorization'],
            recommended_profile: 'fullstack',
            similarity_ai: 0.6,
            similarity_fullstack: 0.62,
            similarity_percentile: 0.4,
            components: { fit: 40 },
            gate_status: 'maybe',
            gate_rule: 'x',
            exclusion_reason: null,
            required_years: null,
            remote_evidence: null,
            remote_regions: [],
            remote_source: null,
          },
        }),
      'POST /api/jobs/5/decision/not-applying': () => ({ job_id: 5, action: 'not_applying', reason: 'wrong_role' }),
      'POST /api/jobs/5/decision/undo': () => ({ job_id: 5, action: 'undo', reverted: 'not_applying' }),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=5')
    const panel = await screen.findByRole('complementary', { name: /job details/i })
    expect(await within(panel).findByText('Mostly support work.')).toBeInTheDocument()
    expect(within(panel).getByText(/requires US work authorization/)).toBeInTheDocument()
    await user.click(within(panel).getByRole('button', { name: 'Not applying' }))
    const dialog = await screen.findByRole('dialog', { name: /Not applying: Support Engineer/ })
    expect(within(dialog).getByRole('button', { name: 'Confirm' })).toBeDisabled()          // a reason is required
    await user.click(within(dialog).getByRole('radio', { name: 'Wrong role' }))
    await user.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(api.lastCall('POST', '/api/jobs/5/decision/not-applying')?.body).toEqual({ reason: 'wrong_role' }))
    await user.click(await screen.findByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(api.callsTo('POST', '/api/jobs/5/decision/undo')).toHaveLength(1))
  })
})

describe('location policy', () => {
  it('sends the location filter and remembers the "show other locations" choice', async () => {
    window.localStorage.clear()
    const user = userEvent.setup()
    const api = mockApi({ 'GET /api/jobs': () => page(), 'GET /api/facets': () => EMPTY_FACETS })
    renderWithProviders(<JobsPage />, '/jobs')
    await screen.findByText('Backend Intern')
    await user.click(within(screen.getByRole('group', { name: 'Location' })).getByRole('checkbox', { name: /^India/ }))
    await waitFor(() => expect(api.lastCall('GET', '/api/jobs')?.url.searchParams.getAll('location_fit')).toEqual(['india']))
    await user.click(screen.getByRole('checkbox', { name: /show other locations/i }))
    await waitFor(() => expect(api.lastCall('GET', '/api/jobs')?.url.searchParams.get('include_other_locations')).toBe('true'))
    expect(window.localStorage.getItem('jobhunt.showOtherLocations')).toBe('1')
  })

  it('shows the location badge and the wrong-location reason', async () => {
    mockApi({
      'GET /api/jobs': () => ({
        total: 1, page: 1, page_size: 25, sort: 'match',
        items: [makeJob({ id: 8, title: 'US Engineer', location_fit: 'wrong_location', location_reason: 'remote, restricted to the US' })],
      }),
      'GET /api/facets': () => EMPTY_FACETS,
    })
    renderWithProviders(<JobsPage />, '/jobs')
    await screen.findByText('US Engineer')
    expect(screen.getByText('Other location')).toBeInTheDocument()
    expect(screen.getByText(/Location: remote, restricted to the US/)).toBeInTheDocument()
  })
})

describe('resume box in the job panel', () => {
  const resume = {
    id: 7, resume_number: 7, code: 'R-0007', version: 2, job_id: 5, job_title: 'AI Engineer', company_name: 'Acme',
    apply_url: 'https://example.test/apply', created_at: '2026-10-19T00:00:00Z', base: 'ai', recipe: 'ai_agents_rag',
    summary_variant: 'S1', fill_ratio: 0.96, page_count: 1, validator_status: 'ok', advice_source: 'gemini',
    model: 'gemini-3.8-flash', cost_usd: 0.002, run_status: 'ok', lifecycle: 'draft', sent_at: null, archived: false,
    has_pdf: true, file_note: null, application_id: null, application_stage: null, parent_resume_id: 6, stale_profile: false,
  }
  const estimate = {
    job_id: 5, model: 'gemini-3.8-flash', estimated_usd: 0.0049, interactive_calls_today: 3, interactive_call_cap: 40,
    blocked: false, message: '', existing_versions: 1, will_use_model: true,
  }
  const base = {
    'GET /api/jobs': () => page([makeJob({ id: 5, title: 'AI Engineer' })]),
    'GET /api/facets': () => EMPTY_FACETS,
    'GET /api/jobs/5': () => makeDetail({ id: 5, title: 'AI Engineer' }),
  }

  it('shows the latest version with links and never tailors or downloads on open', async () => {
    const api = mockApi({
      ...base,
      'GET /api/resumes': () => ({ total: 1, items: [resume], counts: { visible: 1 } }),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=5')
    const box = await screen.findByRole('region', { name: 'Resume' })
    expect(await within(box).findByText('R-0007')).toBeInTheDocument()
    expect(within(box).getByRole('link', { name: 'View' })).toHaveAttribute('href', '/resumes?resume=7')
    expect(within(box).getByRole('link', { name: 'Download' })).toHaveAttribute('href', '/api/resumes/7/download')
    expect(within(box).getByRole('link', { name: /All versions/ })).toBeInTheDocument()
    expect(within(box).getByText(/Gemini advised \(gemini-3\.8-flash, \$0\.002\)/)).toBeInTheDocument()
    expect(within(box).getByText(/never written by the model/)).toBeInTheDocument()
    expect(box.querySelector('iframe, object, embed')).toBeNull()
    expect(api.calls.filter((c) => c.method !== 'GET')).toEqual([])
    expect(api.calls.some((c) => /\/(pdf|preview|download)$/.test(c.url.pathname))).toBe(false)
    expect(api.calls.some((c) => c.url.pathname.endsWith('/tailor'))).toBe(false)
  })

  it('offers Tailor with the estimate when there is none, and posts only on click', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      ...base,
      'GET /api/resumes': () => ({ total: 0, items: [], counts: { visible: 0 } }),
      'GET /api/jobs/5/tailor-estimate': () => ({ ...estimate, existing_versions: 0 }),
      'POST /api/jobs/5/tailor': () => ({ ...resume, status: 'ok', mode: 'advised' }),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=5')
    const box = await screen.findByRole('region', { name: 'Resume' })
    expect(await within(box).findByTestId('tailor-estimate')).toHaveTextContent(/\$0\.005.*3\/40/)
    expect(api.callsTo('POST', '/api/jobs/5/tailor')).toHaveLength(0)
    await user.selectOptions(within(box).getByRole('combobox'), 'fullstack')
    await user.click(within(box).getByRole('button', { name: 'Tailor' }))
    await waitFor(() => expect(api.lastCall('POST', '/api/jobs/5/tailor')?.body).toEqual({ base: 'fullstack' }))
  })

  it('re-tailor asks for confirmation with the estimate and then creates a new version (force)', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      ...base,
      'GET /api/resumes': () => ({ total: 1, items: [resume], counts: { visible: 1 } }),
      'GET /api/jobs/5/tailor-estimate': () => ({ ...estimate, blocked: true, will_use_model: false, estimated_usd: 0,
        message: 'interactive limit reached today (40/40), resets 05:30 IST', interactive_calls_today: 40 }),
      'POST /api/jobs/5/tailor': () => ({ ...resume, id: 8, status: 'ok', mode: 'deterministic' }),
    })
    renderWithProviders(<JobsPage />, '/jobs?job=5')
    const box = await screen.findByRole('region', { name: 'Resume' })
    await user.click(await within(box).findByRole('button', { name: 'Re-tailor' }))
    expect(api.callsTo('POST', '/api/jobs/5/tailor')).toHaveLength(0)
    expect(await within(box).findByTestId('tailor-estimate')).toHaveTextContent(/interactive limit reached today.*resets 05:30 IST.*40\/40/)
    await user.click(within(box).getByRole('button', { name: /Confirm re-tailor/ }))
    await waitFor(() => expect(api.lastCall('POST', '/api/jobs/5/tailor')?.body).toEqual({ base: 'ai', force: true }))
  })
})
