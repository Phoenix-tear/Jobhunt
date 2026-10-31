import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { mockApi, renderWithProviders } from '../test/utils'
import { ResumesPage } from './ResumesPage'

const item = {
  id: 7, resume_number: 7, code: 'R-0007', version: 2, job_id: 5, job_title: 'AI Engineer', company_name: 'Acme',
  apply_url: 'https://example.test/apply', created_at: '2026-10-19T00:00:00Z', base: 'ai', recipe: 'ai_agents_rag',
  summary_variant: 'S1', fill_ratio: 0.96, page_count: 1, validator_status: 'ok', advice_source: 'gemini',
  model: 'gemini-3.8-flash', cost_usd: 0.002, run_status: 'ok', lifecycle: 'draft', sent_at: null, archived: false,
  has_pdf: true, file_note: null, application_id: 3, application_stage: 'saved', parent_resume_id: 6, stale_profile: false,
}
const detail = {
  ...item,
  change_summary: {}, change_text: ['Projects: swapped BetaPortal for AlphaAgent.', 'Keywords: 4 of 6 matched.'],
  diff_vs_previous: 'v2 vs v1: swapped BetaPortal for AlphaAgent, +2 bullets', keywords_matched: ['python', 'rag'],
  keywords_missing: ['pytorch'], validation: { ok: true, violations: [] }, message: '', dropped: [], has_tex: true,
  pdf_path: 'C:/x/resume.pdf', download_name: 'Resume_R0007_Acme_AI_Engineer.pdf',
  how_made: 'Gemini advised (gemini-3.8-flash, $0.002). The resume text is never written by the model.',
  profile_hash_recorded: true,
}
const handlers = () => ({
  'GET /api/resumes': () => ({ total: 1, items: [item], counts: { visible: 1 } }),
  'GET /api/resumes/7': () => detail,
})

describe('Resumes page', () => {
  it('lists versions with number, job, status and the tracker link', async () => {
    mockApi(handlers())
    renderWithProviders(<ResumesPage />, '/resumes')
    const row = (await screen.findByText('AI Engineer')).closest('tr') as HTMLElement
    expect(within(row).getByText('R-0007')).toBeInTheDocument()
    expect(within(row).getByText('Acme')).toBeInTheDocument()
    expect(within(row).getByRole('link', { name: /Open posting/ })).toHaveAttribute('href', 'https://example.test/apply')
    expect(within(row).getByText('draft')).toBeInTheDocument()
    expect(within(row).getByText('validated ✓')).toBeInTheDocument()
    expect(within(row).getByText('Gemini $0.002')).toBeInTheDocument()
    expect(within(row).getByRole('link', { name: /Used in application/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Export CSV' })).toHaveAttribute('href', '/api/resumes/export.csv')
  })

  it('opens a row in an inline preview with what changed, and never downloads by itself', async () => {
    const user = userEvent.setup()
    const api = mockApi(handlers())
    renderWithProviders(<ResumesPage />, '/resumes')
    await user.click(await screen.findByRole('button', { name: 'R-0007' }))
    const pane = await screen.findByRole('region', { name: 'Resume details' })
    const frame = within(pane).getByTitle('Preview of R-0007')
    expect(frame.tagName).toBe('IFRAME')
    expect(frame).toHaveAttribute('src', '/api/resumes/7/preview')
    expect(within(pane).getByText(/swapped BetaPortal for AlphaAgent\.$/)).toBeInTheDocument()
    expect(within(pane).getByText(/v2 vs v1: swapped BetaPortal for AlphaAgent, \+2 bullets/)).toBeInTheDocument()
    expect(within(pane).getByText(/Missing \(not on your resume\): pytorch/)).toBeInTheDocument()
    expect(within(pane).getByText(/never written by the model/)).toBeInTheDocument()
    expect(within(pane).getByRole('link', { name: 'Download' })).toHaveAttribute('href', '/api/resumes/7/download')
    expect(api.calls.some((c) => c.url.pathname.endsWith('/download'))).toBe(false)
    expect(api.calls.filter((c) => c.method !== 'GET')).toEqual([])
  })

  it('buttons call the right endpoints: approve, mark sent, archive, and confirmed re-tailor', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      ...handlers(),
      'POST /api/tailored/7/approve': () => ({ ...item, approved: true }),
      'POST /api/resumes/7/sent': () => ({ application_id: 3, sent_at: 'x' }),
      'POST /api/resumes/7/archive': () => ({ id: 7, archived: true }),
      'GET /api/jobs/5/tailor-estimate': () => ({
        job_id: 5, model: 'gemini-3.8-flash', estimated_usd: 0.005, interactive_calls_today: 4, interactive_call_cap: 40,
        blocked: false, message: '', existing_versions: 2, will_use_model: true,
      }),
      'POST /api/jobs/5/tailor': () => ({ ...item, id: 8, status: 'ok', mode: 'advised' }),
    })
    renderWithProviders(<ResumesPage />, '/resumes?resume=7')
    const pane = await screen.findByRole('region', { name: 'Resume details' })
    await user.click(within(pane).getByRole('button', { name: 'Mark approved' }))
    await waitFor(() => expect(api.lastCall('POST', '/api/tailored/7/approve')?.body).toEqual({ approved: true }))
    await user.click(within(pane).getByRole('button', { name: 'Mark sent' }))
    await waitFor(() => expect(api.callsTo('POST', '/api/resumes/7/sent')).toHaveLength(1))
    await user.click(within(pane).getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(api.lastCall('POST', '/api/resumes/7/archive')?.body).toEqual({ archived: true }))

    await user.click(within(pane).getByRole('button', { name: 'Re-tailor' }))
    expect(api.callsTo('POST', '/api/jobs/5/tailor')).toHaveLength(0)             // nothing yet: confirm first
    expect(await within(pane).findByTestId('tailor-estimate')).toHaveTextContent(/\$0\.005.*4\/40/)
    await user.click(within(pane).getByRole('button', { name: /Confirm re-tailor/ }))
    await waitFor(() => expect(api.lastCall('POST', '/api/jobs/5/tailor')?.body).toEqual({ base: 'ai', force: true }))
  })

  it('filters by company text and status through the query string', async () => {
    const user = userEvent.setup()
    const api = mockApi(handlers())
    renderWithProviders(<ResumesPage />, '/resumes')
    await screen.findByText('AI Engineer')
    await user.type(screen.getByLabelText('Company'), 'acme')
    await user.selectOptions(screen.getByLabelText('Status'), 'sent')
    await waitFor(() => {
      const last = api.calls.filter((c) => c.url.pathname === '/api/resumes').at(-1)
      expect(last?.url.searchParams.get('company')).toBe('acme')
      expect(last?.url.searchParams.get('status')).toBe('sent')
    })
  })
})
