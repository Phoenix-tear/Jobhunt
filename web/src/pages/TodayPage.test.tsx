import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ScheduleSection } from '../components/ScheduleSection'
import { makeJob, mockApi, renderWithProviders } from '../test/utils'
import { TodayPage } from './TodayPage'

function todayJob(id: number, title: string, extra: Record<string, unknown> = {}) {
  return {
    ...makeJob({ id, title, location_fit: 'india', seniority_level: 'junior', match_score: 80 }),
    apply_url: `https://example.test/jobs/${id}`,
    opened_at: null,
    closed_at: null,
    deadline_at: null,
    tailored_resume_id: null,
    approved_pdf: false,
    applied_at: null,
    follow_up_at: null,
    days_since_change: null,
    next_date: null,
    contact_name: null,
    ...extra,
  }
}

const VIEW = {
  generated_at: '2026-10-15T09:00:00Z',
  since: '2026-10-14T09:00:00Z',
  new_total: 1,
  new_jobs: [todayJob(1, 'New ML Role')],
  top: [todayJob(2, 'Top Backend Role')],
  closing_soon: [todayJob(3, 'Closing Role', { deadline_at: '2026-10-18T00:00:00Z' })],
  disappeared: [],
  follow_ups: [todayJob(4, 'Followup Role', { days_since_change: 9 })],
  interviews: [],
  approved_not_applied: [],
  sources: [{ source: 'greenhouse', new_today: 4, relevant_today: 2, open_total: 100 }],
}

describe('Today page', () => {
  it('shows the sections, badges and per-source counts', async () => {
    mockApi({ 'GET /api/today': () => VIEW })
    renderWithProviders(<TodayPage />, '/today')
    const fresh = await screen.findByRole('region', { name: 'New relevant jobs' })
    expect(within(fresh).getByText('New ML Role')).toBeInTheDocument()
    expect(within(fresh).getByText('India')).toBeInTheDocument()
    expect(within(fresh).getByText('Junior')).toBeInTheDocument()
    expect(screen.getByText(/4 new today · 2 relevant/)).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Follow up' })).getByText(/9 days without a change/)).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Closing soon' })).getByText('Closing Role')).toBeInTheDocument()
  })

  it('hides jobs with a caution signal only when the filter is on', async () => {
    const view = {
      ...VIEW,
      new_total: 2,
      new_jobs: [todayJob(1, 'Clean Role'), todayJob(5, 'Caution Role', { has_caution_signal: true })],
    }
    mockApi({ 'GET /api/today': () => view })
    renderWithProviders(<TodayPage />, '/today')
    const fresh = await screen.findByRole('region', { name: 'New relevant jobs' })
    expect(within(fresh).getByText('Caution Role')).toBeInTheDocument()
    expect(within(fresh).getByRole('img', { name: /posting caution/i })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: /hide jobs with a posting caution/i }))
    await waitFor(() => expect(within(fresh).queryByText('Caution Role')).not.toBeInTheDocument())
    expect(within(fresh).getByText('Clean Role')).toBeInTheDocument()
  })

  it('records the open click and uses the Save / Applied / Not applying actions', async () => {
    const api = mockApi({
      'GET /api/today': () => VIEW,
      'GET /api/resumes': () => ({ total: 0, items: [], counts: {} }),
      'POST /api/jobs/1/opened': () => ({ job_id: 1, opened_at: 'x' }),
      'POST /api/jobs/1/decision/save': () => ({ job_id: 1, action: 'saved' }),
      'POST /api/jobs/1/decision/not-applying': () => ({ job_id: 1, action: 'not_applying', reason: 'wrong_domain_or_stack' }),
    })
    renderWithProviders(<TodayPage />, '/today')
    const fresh = await screen.findByRole('region', { name: 'New relevant jobs' })
    fireEvent.click(within(fresh).getByRole('link', { name: /Open apply page/ }))
    await waitFor(() => expect(api.callsTo('POST', '/api/jobs/1/opened')).toHaveLength(1))
    fireEvent.click(within(fresh).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.callsTo('POST', '/api/jobs/1/decision/save')).toHaveLength(1))
    expect(within(fresh).getByRole('button', { name: 'Applied' })).toBeInTheDocument()
    fireEvent.click(within(fresh).getByRole('button', { name: 'Not applying' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Wrong domain or stack' }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(api.lastCall('POST', '/api/jobs/1/decision/not-applying')?.body).toEqual({ reason: 'wrong_domain_or_stack' }))
  })

  it('hides likely-ineligible jobs by default and shows them on request; shows the week strip', async () => {
    const view = {
      ...VIEW,
      new_total: 2,
      new_jobs: [todayJob(1, 'Normal Role'), todayJob(8, 'PhD Only Role', { likely_ineligible: 'Likely not eligible: PhD required' })],
      week: { applied: 3, saved: 2, not_applying: 5, follow_ups_due: 1 },
    }
    mockApi({ 'GET /api/today': () => view })
    renderWithProviders(<TodayPage />, '/today')
    const fresh = await screen.findByRole('region', { name: 'New relevant jobs' })
    expect(within(fresh).getByText('Normal Role')).toBeInTheDocument()
    expect(within(fresh).queryByText('PhD Only Role')).toBeNull()
    expect(screen.getByLabelText('This week')).toHaveTextContent('3 applied, 2 saved, 5 not applying, follow-ups due 1')
    fireEvent.click(screen.getByRole('checkbox', { name: /hide likely-ineligible jobs/i }))
    expect(await within(fresh).findByText('PhD Only Role')).toBeInTheDocument()
    expect(within(fresh).getByText('Likely not eligible: PhD required')).toBeInTheDocument()
  })
})

describe('schedule section', () => {
  it('shows the Gmail banner with the re-auth command and a red missed warning', () => {
    renderWithProviders(
      <ScheduleSection
        schedule={{
          gmail_auth_expired: true,
          gmail_auth_reason: 'revoked',
          gmail_auth_since: null,
          gmail_reauth_command: '.\.venv\Scripts\python.exe -m jobhunt gmail-auth',
          tasks: [
            {
              task: 'ingest', name: 'jobhunt-ingest', description: '', schedule: 'daily at 06:30', heavy: true,
              last_run_at: '2026-10-10T01:00:00Z', last_status: 'ok', last_duration_seconds: 61, last_detail: null,
              last_ok_at: '2026-10-10T01:00:00Z', next_run_at: '2026-10-16T01:00:00Z', missed_runs: 5, missed_warning: true,
            },
          ],
        }}
      />,
    )
    expect(screen.getByText(/jobhunt gmail-auth/)).toBeInTheDocument()
    expect(screen.getByText('missed 5')).toBeInTheDocument()
    expect(screen.getByText('61 s')).toBeInTheDocument()
  })
})
