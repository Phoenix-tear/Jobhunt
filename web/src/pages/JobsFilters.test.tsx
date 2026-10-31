import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { EMPTY_FACETS, makeJob, mockApi, renderWithProviders } from '../test/utils'
import { JobsPage } from './JobsPage'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function location(): string {
  return screen.getByTestId('location').textContent ?? ''
}

const RELEVANT = [makeJob({ id: 1, title: 'Backend Intern' }), makeJob({ id: 2, title: 'Data Engineer' })]
const EVERYTHING = [...RELEVANT, makeJob({ id: 3, title: 'Java Developer' }), makeJob({ id: 4, title: 'Sales Lead' }), makeJob({ id: 5, title: 'Chef' })]

/** Answers like the real API: the category decides WHICH jobs, the sort only their order. */
function categoryAwareApi() {
  return mockApi({
    'GET /api/jobs': ({ url }) => {
      const category = url.searchParams.get('category')
      const items = category === 'all' ? EVERYTHING : category === 'target' ? RELEVANT.slice(0, 1) : RELEVANT
      return { total: items.length, page: 1, page_size: 25, sort: url.searchParams.get('sort') ?? 'match', items }
    },
    'GET /api/facets': () => EMPTY_FACETS,
  })
}

describe('category, view, freshness and sort are independent controls', () => {
  it('picking Newest keeps the Relevant category and the list count', async () => {
    const user = userEvent.setup()
    const api = categoryAwareApi()
    renderWithProviders(<JobsPage />, '/jobs')
    await screen.findByText('Backend Intern')
    expect(screen.getByTestId('result-count')).toHaveTextContent('2 jobs')

    await user.selectOptions(screen.getByRole('combobox', { name: /sort/i }), 'newest_posted')

    await waitFor(() => expect(api.lastCall('GET', '/api/jobs')?.url.searchParams.get('sort')).toBe('newest_posted'))
    const last = api.lastCall('GET', '/api/jobs')!.url.searchParams
    expect(last.get('category')).toBe('relevant') // the category did not change
    expect(last.get('relevant_only')).toBeNull()
    expect(screen.getByRole('radio', { name: /^relevant/i })).toBeChecked()
    expect(screen.getByTestId('result-count')).toHaveTextContent('2 jobs') // same count, not "all jobs"
    expect(screen.queryByText('Chef')).not.toBeInTheDocument()
    expect(location()).toBe('/jobs?sort=newest_posted')
  })

  it('every sort leaves the category, view, location and freshness alone', async () => {
    const user = userEvent.setup()
    const api = categoryAwareApi()
    renderWithProviders(<JobsPage />, '/jobs?category=target&view=saved&loc=india&fresh=fresh')
    await screen.findByText('Backend Intern')
    for (const sort of ['newest_posted', 'newest_seen', 'oldest', 'company', 'title', 'match']) {
      await user.selectOptions(screen.getByRole('combobox', { name: /sort/i }), sort)
      await waitFor(() => expect(api.lastCall('GET', '/api/jobs')?.url.searchParams.get('sort')).toBe(sort))
      const last = api.lastCall('GET', '/api/jobs')!.url.searchParams
      expect([last.get('category'), last.get('view'), last.getAll('location_fit'), last.getAll('freshness')]).toEqual([
        'target',
        'saved',
        ['india'],
        ['fresh'],
      ])
    }
  })

  it('changing a filter keeps the chosen sort', async () => {
    const user = userEvent.setup()
    const api = categoryAwareApi()
    renderWithProviders(<JobsPage />, '/jobs?sort=oldest')
    await screen.findByText('Backend Intern')
    await user.click(screen.getByRole('radio', { name: /^all$/i }))
    await waitFor(() => expect(api.lastCall('GET', '/api/jobs')?.url.searchParams.get('category')).toBe('all'))
    expect(api.lastCall('GET', '/api/jobs')?.url.searchParams.get('sort')).toBe('oldest')
    expect(screen.getByRole('combobox', { name: /sort/i })).toHaveValue('oldest')
    await user.click(screen.getByRole('button', { name: /^Saved/ }))
    await user.click(screen.getByRole('checkbox', { name: /^Fresh/ }))
    await waitFor(() => expect(location()).toContain('fresh=fresh'))
    expect(location()).toContain('sort=oldest')
    expect(location()).toContain('category=all')
    expect(location()).toContain('view=saved')
  })

  it('remembers category, view, freshness and sort in localStorage and restores them on a bare URL', async () => {
    const user = userEvent.setup()
    categoryAwareApi()
    const first = renderWithProviders(<JobsPage />, '/jobs')
    await screen.findByText('Backend Intern')
    await user.click(screen.getByRole('radio', { name: /^maybe/i }))
    await user.selectOptions(screen.getByRole('combobox', { name: /sort/i }), 'company')
    await waitFor(() =>
      expect(JSON.parse(window.localStorage.getItem('jobhunt.jobFilters') ?? '{}')).toMatchObject({ category: 'maybe', sort: 'company' }),
    )
    first.unmount()

    const api = categoryAwareApi()
    renderWithProviders(<JobsPage />, '/jobs')
    await waitFor(() => expect(api.lastCall('GET', '/api/jobs')?.url.searchParams.get('category')).toBe('maybe'))
    expect(api.lastCall('GET', '/api/jobs')?.url.searchParams.get('sort')).toBe('company')
  })

  it('the URL wins over what was remembered', async () => {
    window.localStorage.setItem('jobhunt.jobFilters', JSON.stringify({ category: 'maybe', sort: 'company' }))
    const api = categoryAwareApi()
    renderWithProviders(<JobsPage />, '/jobs?category=all&sort=title')
    await screen.findByText('Backend Intern')
    expect(api.lastCall('GET', '/api/jobs')?.url.searchParams.get('category')).toBe('all')
    expect(api.lastCall('GET', '/api/jobs')?.url.searchParams.get('sort')).toBe('title')
  })

  it('works when localStorage is blocked or holds garbage', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    categoryAwareApi()
    const user = userEvent.setup()
    renderWithProviders(<JobsPage />, '/jobs')
    await screen.findByText('Backend Intern')
    await user.selectOptions(screen.getByRole('combobox', { name: /sort/i }), 'oldest')
    await waitFor(() => expect(location()).toBe('/jobs?sort=oldest'))
    vi.restoreAllMocks()
    window.localStorage.setItem('jobhunt.jobFilters', '{not json')
    renderWithProviders(<JobsPage />, '/jobs')
    expect((await screen.findAllByText('Backend Intern')).length).toBeGreaterThan(0)
  })

  it('shows the Auto-hidden tab with Restore, and the age chips', async () => {
    const user = userEvent.setup()
    const hidden = makeJob({ id: 7, title: 'Old Backend Role', auto_hidden: 'auto_stale', freshness: 'stale', age_days: 120 })
    const api = mockApi({
      'GET /api/jobs': ({ url }) => {
        const items = url.searchParams.get('view') === 'auto_hidden' ? [hidden] : [makeJob({ id: 8, title: 'Fresh Role', freshness: 'unknown' })]
        return { total: items.length, page: 1, page_size: 25, sort: 'match', items }
      },
      'GET /api/facets': () => EMPTY_FACETS,
      'POST /api/jobs/7/restore': () => ({ job_id: 7, restored: true }),
    })
    renderWithProviders(<JobsPage />, '/jobs')
    expect(await screen.findByText('Fresh Role')).toBeInTheDocument()
    expect(screen.getByText('age unknown')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /^Auto-hidden/ }))
    expect(await screen.findByText('Old Backend Role')).toBeInTheDocument()
    expect(screen.getByText(/Auto-hidden: too old/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(api.callsTo('POST', '/api/jobs/7/restore')).toHaveLength(1))
  })
})
