import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockApi, renderWithProviders } from '../test/utils'
import { SetupWizardPage } from './SetupWizardPage'
import { SettingsPage } from './SettingsPage'
import { DemoBanner } from '../components/DemoBanner'

describe('v12 Setup Wizard Page', () => {
  it('renders welcome step and allows entering demo mode', async () => {
    const api = mockApi({
      'GET /api/setup/state': () => ({
        completed: false,
        step: 'welcome',
        completed_at: null,
        schema_version: 1,
        demo_mode: false,
      }),
      'GET /api/profile/role-families/presets': () => ({
        presets: [{ id: 'fullstack', label: 'Full-Stack' }],
      }),
      'GET /api/resumes/bases': () => ({
        bases: [{ id: 'cv', path: 'data/resumes/cv.tex' }],
      }),
      'POST /api/setup/step': () => ({ status: 'ok' }),
      'POST /api/setup/demo/enter': () => ({
        demo_mode: true,
        active_database: 'data/demo/demo.sqlite3',
        persona: 'Alex Chen',
      }),
    })

    renderWithProviders(<SetupWizardPage />, '/setup')

    expect(await screen.findByText(/Welcome to jobhunt/i)).toBeInTheDocument()
    expect(screen.getByText(/Try Demo Mode Instead/i)).toBeInTheDocument()

    // Click demo mode button
    fireEvent.click(screen.getByText(/Try Demo Mode Instead/i))
    await waitFor(() => {
      expect(api.callsTo('POST', '/api/setup/demo/enter')).toHaveLength(1)
    })
  })

  it('navigates between steps via Next and Back buttons', async () => {
    mockApi({
      'GET /api/setup/state': () => ({
        completed: false,
        step: 'welcome',
        completed_at: null,
        schema_version: 1,
        demo_mode: false,
      }),
      'GET /api/profile/role-families/presets': () => ({ presets: [] }),
      'GET /api/resumes/bases': () => ({ bases: [] }),
      'POST /api/setup/step': () => ({ status: 'ok' }),
    })

    renderWithProviders(<SetupWizardPage />, '/setup')
    expect(await screen.findByText(/Welcome to jobhunt/i)).toBeInTheDocument()

    // Click Set Up My Own Job Hunt to go to Gemini step
    fireEvent.click(screen.getByText(/Set Up My Own Job Hunt/i))
    expect(await screen.findByText(/1\. Gemini API Key/i)).toBeInTheDocument()

    // Click back
    fireEvent.click(screen.getByText(/← Back/i))
    expect(await screen.findByText(/Welcome to jobhunt/i)).toBeInTheDocument()
  })
})

describe('v12 Settings Page', () => {
  it('displays masked secret info and switches between tabs', async () => {
    mockApi({
      'GET /api/settings/secrets-status': () => ({ gemini_key: { present: true, last4: '9876' } }),
      'GET /api/settings/models': () => ({ live_testing_available: true, models: ['gemini-2.5-flash'] }),
      'GET /api/settings/budget': () => ({
        preset: 'free',
        daily_pipeline_calls: 15,
        daily_interactive_calls: 5,
        monthly_budget_usd: 0.0,
        usage: { today: { total_calls: 3, pipeline_calls: 3, interactive_calls: 0, cost_usd: 0.0 }, month: { total_calls: 12, cost_usd: 0.0 } },
      }),
      'GET /api/profile/role-families/presets': () => ({ presets: [{ id: 'fullstack', label: 'Full-Stack' }] }),
      'GET /api/profile/role-families': () => ({ active_families: ['fullstack'] }),
      'GET /api/resumes/bases': () => ({ bases: [{ id: 'cv', path: 'data/resumes/cv.tex' }] }),
      'GET /api/facts': () => ({ facts: [{ id: 'fact-1', text: 'Shipped high scale backend', ticked: true, edited: false }] }),
      'GET /api/profile/summaries': () => ({ summaries: { fullstack: 'Experienced full stack developer' } }),
      'GET /api/profile/answers': () => ({ answers: { work_authorization: 'Authorized', sponsorship: 'Not Required' } }),
      'GET /api/settings/sources': () => ({ sources: { simplify: true, ashby: true } }),
      'GET /api/settings/schedule': () => ({ schedule: { ingest: { enabled: true, cron: '0 8 * * *' } } }),
      'GET /api/settings/gmail': () => ({ enabled: false, token_present: false }),
    })

    renderWithProviders(<SettingsPage />, '/settings?tab=gemini')

    // Masked key display
    expect(await screen.findByText(/Present \(ending in 9876\)/i)).toBeInTheDocument()

    // Switch to Budget tab
    fireEvent.click(screen.getByRole('button', { name: /^Budget$/i }))
    expect(await screen.findByText(/Usage, Rates & Monthly Budget/i)).toBeInTheDocument()

    // Switch to Sources tab
    fireEvent.click(screen.getByRole('button', { name: /^Sources$/i }))
    expect(await screen.findByText(/Job Feeds & ATS Adapters/i)).toBeInTheDocument()
  })

  it('allows toggling and editing facts in-place', async () => {
    const api = mockApi({
      'GET /api/settings/gemini-key': () => ({ present: false, last4: '' }),
      'GET /api/settings/models': () => ({ live_testing_available: true, models: [] }),
      'GET /api/settings/budget': () => ({ preset: 'free', daily_pipeline_calls: 15, daily_interactive_calls: 5, monthly_budget_usd: 0.0 }),
      'GET /api/profile/role-families/presets': () => ({ presets: [] }),
      'GET /api/profile/role-families': () => ({ active_families: [] }),
      'GET /api/resumes/bases': () => ({ bases: [] }),
      'GET /api/facts': () => ({
        facts: [{ id: 'fact-1', text: 'Built React interface', ticked: true, edited: false }],
        total_count: 1,
        ticked_count: 1,
      }),
      'POST /api/facts/tick': () => ({ fact_id: 'fact-1', ticked: false, updated_count: 0 }),
      'POST /api/facts/edit': () => ({ success: true, new_id: 'fact-1-edited', text: 'Built advanced React interface' }),
      'GET /api/profile/summaries': () => ({ summaries: {} }),
      'GET /api/profile/answers': () => ({ answers: {} }),
      'GET /api/settings/sources': () => ({ sources: {} }),
      'GET /api/settings/schedule': () => ({ schedule: {} }),
      'GET /api/settings/gmail': () => ({ enabled: false, token_present: false }),
    })

    renderWithProviders(<SettingsPage />, '/settings?tab=facts')

    expect(await screen.findByText(/Built React interface/i)).toBeInTheDocument()

    // Toggle tick
    const checkbox = screen.getByRole('checkbox')
    expect(checkbox).toBeChecked()
    fireEvent.click(checkbox)
    await waitFor(() => {
      expect(api.callsTo('POST', '/api/facts/tick')).toHaveLength(1)
    })

    // Edit button
    const editBtn = screen.getByText('✎')
    fireEvent.click(editBtn)
    const textarea = screen.getByDisplayValue('Built React interface')
    fireEvent.change(textarea, { target: { value: 'Built advanced React interface' } })
    fireEvent.click(screen.getByText('Save Edit'))

    await waitFor(() => {
      expect(api.callsTo('POST', '/api/facts/edit')).toHaveLength(1)
    })
  })
})

describe('v12 DemoBanner', () => {
  it('renders demo notification banner and calls exit API on click', async () => {
    const api = mockApi({
      'POST /api/setup/demo/exit': () => ({ demo_mode: false }),
    })

    let exited = false
    renderWithProviders(<DemoBanner onExit={() => { exited = true }} />)

    expect(screen.getByRole('banner')).toBeInTheDocument()
    expect(screen.getByText(/Demo Mode Active \(Alex Chen Persona\)/i)).toBeInTheDocument()

    const exitBtn = screen.getByRole('button', { name: /Exit Demo Mode/i })
    fireEvent.click(exitBtn)

    await waitFor(() => {
      expect(api.callsTo('POST', '/api/setup/demo/exit')).toHaveLength(1)
      expect(exited).toBe(true)
    })
  })
})
