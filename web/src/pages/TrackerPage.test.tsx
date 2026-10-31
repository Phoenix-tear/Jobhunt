import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { BoardCard } from '../api/types'
import { STAGES } from '../api/types'
import { jsonResponse, makeCard, makeDetail, mockApi, renderWithProviders } from '../test/utils'
import { TrackerPage } from './TrackerPage'

afterEach(() => {
  vi.unstubAllGlobals()
})

function boardOf(cards: BoardCard[]) {
  return {
    total: cards.length,
    columns: STAGES.map((stage) => {
      const inStage = cards.filter((card) => card.application.stage === stage)
      return { stage, count: inStage.length, cards: inStage }
    }),
  }
}

const column = (name: RegExp | string) => screen.getByRole('listitem', { name })

describe('TrackerPage', () => {
  it('shows every stage column with its cards and highlights overdue follow-ups', async () => {
    const overdue = makeCard(2, 'applied')
    overdue.application.follow_up_at = '2020-01-01'
    const upcoming = makeCard(3, 'saved')
    upcoming.application.follow_up_at = '2999-01-01'
    mockApi({ 'GET /api/applications': () => boardOf([makeCard(1, 'saved'), overdue, upcoming]) })
    renderWithProviders(<TrackerPage />, '/tracker')

    await screen.findByText('Role 1')
    for (const name of ['Saved column', 'Applied column', 'OA / Interview column', 'Offer column', 'Rejected column']) {
      expect(column(name)).toBeInTheDocument()
    }
    expect(within(column('Saved column')).getByText('Role 1')).toBeInTheDocument()
    expect(within(column('Applied column')).getByText('Role 2')).toBeInTheDocument()

    const overdueCard = within(column('Applied column')).getByRole('article')
    expect(overdueCard).toHaveAttribute('data-overdue', 'true')
    expect(within(overdueCard).getByText('Follow-up overdue')).toBeInTheDocument()
    expect(screen.getByRole('article', { name: /Role 3/ })).toHaveAttribute('data-overdue', 'false')
  })

  it('moves a card with the stage control: optimistic update, then the PATCH', async () => {
    const user = userEvent.setup()
    let finishRequest: () => void = () => undefined
    const api = mockApi({
      'GET /api/applications': () => boardOf([makeCard(1, 'saved')]),
      'PATCH /api/applications/1': () =>
        new Promise<Response>((resolve) => {
          finishRequest = () => resolve(jsonResponse({ ...makeCard(1, 'applied').application, applied_at: '2026-10-12T00:00:00Z' }))
        }),
    })
    renderWithProviders(<TrackerPage />, '/tracker')
    await screen.findByText('Role 1')

    await user.selectOptions(screen.getByLabelText('Move to'), 'applied')

    // optimistic: already in the Applied column while the request is still in flight
    await waitFor(() => expect(within(column('Applied column')).getByText('Role 1')).toBeInTheDocument())
    expect(within(column('Saved column')).queryByText('Role 1')).toBeNull()
    expect(api.lastCall('PATCH', '/api/applications/1')!.body).toEqual({ stage: 'applied' })
    finishRequest()
    await waitFor(() => expect(api.callsTo('GET', '/api/applications').length).toBeGreaterThan(1)) // refetched
  })

  it('rolls the card back and shows an error when the move fails', async () => {
    const user = userEvent.setup()
    mockApi({
      'GET /api/applications': () => boardOf([makeCard(1, 'saved')]),
      'PATCH /api/applications/1': () => jsonResponse({ detail: 'database is locked' }, 500),
    })
    renderWithProviders(<TrackerPage />, '/tracker')
    await screen.findByText('Role 1')

    await user.selectOptions(screen.getByLabelText('Move to'), 'offer')

    expect(await screen.findByText(/Could not move “Role 1” to Offer: database is locked/)).toBeInTheDocument()
    await waitFor(() => expect(within(column('Saved column')).getByText('Role 1')).toBeInTheDocument())
    expect(within(column('Offer column')).queryByText('Role 1')).toBeNull()
    expect(screen.getByLabelText('Move to')).toHaveValue('saved')
  })

  it('saves notes on blur and nothing when they did not change', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      'GET /api/applications': () => boardOf([makeCard(1, 'applied')]),
      'PATCH /api/applications/1': () => makeCard(1, 'applied').application,
    })
    renderWithProviders(<TrackerPage />, '/tracker')
    await screen.findByText('Role 1')
    const notes = screen.getByLabelText('Notes')

    await user.click(notes)
    await user.tab() // blur without typing
    expect(api.callsTo('PATCH', '/api/applications/1')).toHaveLength(0)

    await user.type(notes, 'Referred by Priya')
    await user.tab()
    await waitFor(() => expect(api.lastCall('PATCH', '/api/applications/1')!.body).toEqual({ notes: 'Referred by Priya' }))
  })

  it('sets and clears the follow-up date', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      'GET /api/applications': () => boardOf([makeCard(1, 'applied')]),
      'PATCH /api/applications/1': () => makeCard(1, 'applied').application,
    })
    renderWithProviders(<TrackerPage />, '/tracker')
    await screen.findByText('Role 1')
    await user.type(screen.getByLabelText('Follow up'), '2026-11-02')
    await waitFor(() => expect(api.lastCall('PATCH', '/api/applications/1')!.body).toEqual({ follow_up_at: '2026-11-02' }))
  })

  it('offers Remove only while a card is in Saved', async () => {
    const user = userEvent.setup()
    let deleted = false
    const api = mockApi({
      'GET /api/applications': () => boardOf(deleted ? [makeCard(2, 'applied')] : [makeCard(1, 'saved'), makeCard(2, 'applied')]),
      'DELETE /api/applications/1': () => {
        deleted = true
        return jsonResponse(null, 204)
      },
    })
    renderWithProviders(<TrackerPage />, '/tracker')
    await screen.findByText('Role 1')
    expect(within(column('Applied column')).queryByRole('button', { name: 'Remove' })).toBeNull()

    await user.click(within(column('Saved column')).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(api.callsTo('DELETE', '/api/applications/1')).toHaveLength(1))
    await waitFor(() => expect(screen.queryByText('Role 1')).toBeNull())
  })

  it('shows the empty state, and an error with retry', async () => {
    mockApi({ 'GET /api/applications': () => boardOf([]) })
    renderWithProviders(<TrackerPage />, '/tracker')
    expect(await screen.findByText('Nothing tracked yet')).toBeInTheDocument()
  })

  it('shows an error state when the board cannot be loaded', async () => {
    mockApi({ 'GET /api/applications': () => jsonResponse({ detail: 'boom' }, 500) })
    renderWithProviders(<TrackerPage />, '/tracker')
    expect(await screen.findByRole('alert')).toHaveTextContent('boom')
  })

  it('renders card text as plain text', async () => {
    mockApi({
      'GET /api/applications': () =>
        boardOf([makeCard(1, 'saved', { title: '<img src=x onerror=alert(1)> Engineer', company_name: '<script>x</script>Acme' })]),
    })
    renderWithProviders(<TrackerPage />, '/tracker')
    expect(await screen.findByText('<img src=x onerror=alert(1)> Engineer')).toBeInTheDocument()
    expect(document.querySelector('img')).toBeNull()
    expect(document.querySelector('script')).toBeNull()
  })
})

describe('Add a job by URL', () => {
  async function openForm() {
    const user = userEvent.setup()
    await screen.findByText('Nothing tracked yet')
    await user.click(screen.getByText('Add a job by URL'))
    return user
  }

  it('posts to the manual endpoint, then saves the job to the board', async () => {
    const api = mockApi({
      'GET /api/applications': () => boardOf([]),
      'POST /api/jobs/manual': () => ({
        created: true,
        job: makeDetail({ id: 77, title: 'Platform Engineer' }),
        ats: { attempted: true, status: 'fetched', detail: 'Platform Engineer' },
      }),
      'POST /api/applications': () => makeCard(9, 'saved').application,
    })
    renderWithProviders(<TrackerPage />, '/tracker')
    const user = await openForm()

    await user.type(screen.getByLabelText('Apply URL'), 'https://boards.greenhouse.io/acme/jobs/1')
    await user.type(screen.getByLabelText('Job title'), 'Platform Engineer')
    await user.type(screen.getByLabelText('Company'), 'Acme')
    await user.click(screen.getByRole('button', { name: 'Add job' }))

    await waitFor(() => expect(api.callsTo('POST', '/api/jobs/manual')).toHaveLength(1))
    expect(api.lastCall('POST', '/api/jobs/manual')!.body).toEqual({
      title: 'Platform Engineer',
      company: 'Acme',
      apply_url: 'https://boards.greenhouse.io/acme/jobs/1',
      fetch_from_ats: true,
    })
    await waitFor(() => expect(api.lastCall('POST', '/api/applications')!.body).toEqual({ job_id: 77 }))
    const messages = await screen.findAllByText(/Job added\..*full posting was fetched.*in Saved/)
    expect(messages.length).toBeGreaterThan(0)
  })

  it('validates locally without calling the API', async () => {
    const api = mockApi({ 'GET /api/applications': () => boardOf([]) })
    renderWithProviders(<TrackerPage />, '/tracker')
    const user = await openForm()

    await user.type(screen.getByLabelText('Apply URL'), 'javascript:alert(1)')
    await user.type(screen.getByLabelText('Job title'), 'X')
    await user.type(screen.getByLabelText('Company'), 'Y')
    await user.click(screen.getByRole('button', { name: 'Add job' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/full http\(s\) link/)
    expect(api.callsTo('POST', '/api/jobs/manual')).toHaveLength(0)
  })

  it('shows the API error message', async () => {
    mockApi({
      'GET /api/applications': () => boardOf([]),
      'POST /api/jobs/manual': () => jsonResponse({ detail: 'apply_url must be a valid http(s) URL' }, 422),
    })
    renderWithProviders(<TrackerPage />, '/tracker')
    const user = await openForm()
    await user.type(screen.getByLabelText('Apply URL'), 'https://example.com/job')
    await user.type(screen.getByLabelText('Job title'), 'T')
    await user.type(screen.getByLabelText('Company'), 'C')
    await user.click(screen.getByRole('button', { name: 'Add job' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('apply_url must be a valid http(s) URL')
  })
})
