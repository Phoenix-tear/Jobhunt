import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockApi, renderWithProviders } from '../test/utils'
import { SetupWizardPage } from './SetupWizardPage'
import { SettingsPage } from './SettingsPage'

describe('v12b Setup Wizard and Settings Safe Migration Tests', () => {
  const defaultWizardApi = () => ({
    'GET /api/setup/state': () => ({
      completed: false,
      step: 'welcome',
      completed_at: null,
      schema_version: 1,
      demo_mode: false,
    }),
    'GET /api/profile/role-families/presets': () => ({
      presets: [
        { id: 'genai', label: 'Generative AI & LLM Agents', keywords: ['rag', 'llm'], title_patterns: ['ai engineer'] },
        { id: 'salesforce', label: 'Salesforce & CRM Engineering', keywords: ['apex', 'salesforce'], title_patterns: ['salesforce developer'] },
        { id: 'fullstack', label: 'Full Stack Software Engineering', keywords: ['react', 'node'], title_patterns: ['fullstack'] },
        { id: 'backend', label: 'Backend Software Engineering', keywords: ['python', 'fastapi'], title_patterns: ['backend'] },
      ],
    }),
    'GET /api/resumes/bases': () => ({
      bases: [
        {
          id: 'ai',
          filename: 'resume_ai.tex',
          exists: true,
          template: {
            template: 'custom_macros',
            can_tailor: true,
            compiles: true,
            parses_facts: true,
            reasons: ['Fully supported custom template'],
          },
        },
        {
          id: 'plain',
          filename: 'resume_plain.tex',
          exists: true,
          template: {
            template: 'unsupported',
            can_tailor: false,
            compiles: true,
            parses_facts: true,
            reasons: ['Template has non-standard environments; tailoring disabled'],
          },
        },
      ],
    }),
    'GET /api/facts': () => ({
      facts: [
        { id: 'T-1111', text: 'Distributed stream processing engine in Rust', kind: 'project_bullet', ticked: true },
        { id: 'T-2222', text: 'Designed high-throughput GraphQL APIs', kind: 'experience_bullet', ticked: true },
        { id: 'T-3333', text: 'Managed production deployment alerts', kind: 'experience_bullet', ticked: false },
      ],
      used_count: 2,
      unused_count: 1,
      facts_hash: 'abc123def456',
    }),
    'POST /api/setup/step': () => ({ status: 'ok' }),
    'POST /api/settings/budget': () => ({ status: 'ok' }),
    'GET /api/settings/budget': () => ({
      budget: { daily_calls: 20, interactive_daily_calls: 15, monthly_usd: 1.0, preset: 'free' },
      usage: { today_calls: 0, today_cost_usd: 0 },
      reset_time_utc: '00:00:00Z',
    }),
    'POST /api/profile/suggest-summaries': () => ({
      variants: [
        { id: 'S1', title: 'Systems Specialist', text: 'Experienced in Rust stream processing and low-latency APIs.' },
        { id: 'S2', title: 'Backend Generalist', text: 'Backend software engineer with experience building GraphQL services.' },
      ],
    }),
    'POST /api/profile/summaries': () => ({ status: 'ok' }),
    'POST /api/settings/discover-company': () => ({
      found: true,
      ats: 'greenhouse',
      company_name: 'Stripe',
      company: { slug: 'stripe', ats_type: 'greenhouse', name: 'Stripe', active: true },
    }),
    'GET /api/health': () => ({
      readiness: { gemini_key: { passed: true, label: 'Gemini API Key' }, authoritative_resume: { passed: true, label: 'Resume' } },
      ollama_online: false,
    }),
    'POST /api/setup/first-ingest': () => ({
      status: 'running',
      progress: 25,
      stage: 'ingest',
      message: 'Fetching initial jobs from remote ATS boards...',
      ollama_status: 'offline (similarity skipped)',
    }),
    'GET /api/setup/ingest-progress': () => ({
      status: 'running',
      progress: 50,
      stage: 'ingest',
      total_jobs: 12,
      elapsed_seconds: 4.5,
      estimated_time_remaining: 15,
      message: 'Fetched 12 postings',
      ollama_status: 'offline (similarity skipped)',
    }),
    'POST /api/setup/ingest-cancel': () => ({
      status: 'cancelled',
      message: 'Cancelled by user',
    }),
  })

  // Test 1: Navigation and welcome
  it('1. renders welcome screen and step indicator', async () => {
    mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    expect(await screen.findByText(/Welcome to jobhunt/i)).toBeInTheDocument()
    expect(screen.getByText(/Set Up My Own Job Hunt/i)).toBeInTheDocument()
  })

  // Test 2: Moving from Welcome to Gemini step
  it('2. advances to Gemini step on Set Up My Own Job Hunt click', async () => {
    const api = mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const startCard = await screen.findByText(/Set Up My Own Job Hunt/i)
    fireEvent.click(startCard)
    await waitFor(() => {
      expect(api.callsTo('POST', '/api/setup/step')).toHaveLength(1)
    })
    expect(await screen.findByText(/1\. Gemini API Key/i)).toBeInTheDocument()
  })

  // Test 3: Budget preset selection (Free vs Paid)
  it('3. renders Free and Paid budget presets with exact limits', async () => {
    mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const budgetStepBtn = await screen.findByRole('button', { name: /3\. Budget/i })
    fireEvent.click(budgetStepBtn)
    expect(await screen.findByText(/Free Tier Only/i)).toBeInTheDocument()
    expect(screen.getByText(/Paid Starter/i)).toBeInTheDocument()
    expect(screen.getByText(/\$1\.00 \/ mo/i)).toBeInTheDocument()
    expect(screen.getByText(/\$5\.00 \/ mo/i)).toBeInTheDocument()
  })

  // Test 4: Saving Free budget preset
  it('4. saves selected budget tier and advances step', async () => {
    const api = mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const budgetStepBtn = await screen.findByRole('button', { name: /3\. Budget/i })
    fireEvent.click(budgetStepBtn)
    const continueBtn = await screen.findByRole('button', { name: /Save & Continue/i })
    fireEvent.click(continueBtn)
    await waitFor(() => {
      expect(api.callsTo('POST', '/api/settings/budget')).toHaveLength(1)
    })
  })

  // Test 5: Resumes step displays template capability badges
  it('5. displays template badges: Full tailoring vs Facts & letters only', async () => {
    mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const resumesStepBtn = await screen.findByRole('button', { name: /4\. Resumes/i })
    fireEvent.click(resumesStepBtn)
    expect(await screen.findByText(/Full Tailoring/i)).toBeInTheDocument()
    expect(screen.getByText(/Facts & Letters Only/i)).toBeInTheDocument()
  })

  // Test 6: Resumes step displays reasons and warnings for unsupported templates
  it('6. shows reasons and warnings for template restrictions', async () => {
    mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const resumesStepBtn = await screen.findByRole('button', { name: /4\. Resumes/i })
    fireEvent.click(resumesStepBtn)
    expect(await screen.findByText(/Template has non-standard environments/i)).toBeInTheDocument()
  })

  // Test 7: Facts review step displays hash, used, and unused counts
  it('7. displays facts_hash, used count, and unused count', async () => {
    mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const factsStepBtn = await screen.findByRole('button', { name: /5\. Facts Review/i })
    fireEvent.click(factsStepBtn)
    expect(await screen.findByText(/2 Ticked/i)).toBeInTheDocument()
    expect(screen.getByText(/1 Unticked/i)).toBeInTheDocument()
    expect(screen.getByText(/abc123def456/i)).toBeInTheDocument()
  })

  // Test 8: Facts review displays fact text items
  it('8. renders fact items with checkboxes', async () => {
    mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const factsStepBtn = await screen.findByRole('button', { name: /5\. Facts Review/i })
    fireEvent.click(factsStepBtn)
    expect(await screen.findByText(/Distributed stream processing engine in Rust/i)).toBeInTheDocument()
    expect(screen.getByText(/Designed high-throughput GraphQL APIs/i)).toBeInTheDocument()
  })

  // Test 9: Summary variants proposal trigger
  it('9. allows requesting summary proposals from ticked facts', async () => {
    const api = mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const factsStepBtn = await screen.findByRole('button', { name: /5\. Facts Review/i })
    fireEvent.click(factsStepBtn)
    const proposeBtn = await screen.findByRole('button', { name: /Suggest Summary Variants/i })
    fireEvent.click(proposeBtn)
    await waitFor(() => {
      expect(api.callsTo('POST', '/api/profile/suggest-summaries')).toHaveLength(1)
    })
    expect(await screen.findByText(/Systems Specialist/i)).toBeInTheDocument()
  })

  // Test 10: Approving summary variants
  it('10. saves approved summary variants to summaries.md', async () => {
    const api = mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const factsStepBtn = await screen.findByRole('button', { name: /5\. Facts Review/i })
    fireEvent.click(factsStepBtn)
    const proposeBtn = await screen.findByRole('button', { name: /Suggest Summary Variants/i })
    fireEvent.click(proposeBtn)
    expect(await screen.findByText(/Systems Specialist/i)).toBeInTheDocument()

    const approveBtn = await screen.findByRole('button', { name: /Approve & Save Summary Variants/i })
    fireEvent.click(approveBtn)
    await waitFor(() => {
      expect(api.callsTo('POST', '/api/profile/summaries')).toHaveLength(1)
    })
  })

  // Test 11: Role family presets display all 10 presets including Salesforce and GenAI
  it('11. displays role family presets catalog including Salesforce and GenAI', async () => {
    mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const profileStepBtn = await screen.findByRole('button', { name: /6\. Target Profile/i })
    fireEvent.click(profileStepBtn)
    expect(await screen.findByText(/Generative AI & LLM Agents/i)).toBeInTheDocument()
    expect(screen.getByText(/Salesforce & CRM Engineering/i)).toBeInTheDocument()
  })

  // Test 12: Custom role family creator opens and allows input
  it('12. provides custom role family creation modal/form', async () => {
    mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const profileStepBtn = await screen.findByRole('button', { name: /6\. Target Profile/i })
    fireEvent.click(profileStepBtn)
    const customBtn = await screen.findByRole('button', { name: /\+ Add Custom Role Family/i })
    fireEvent.click(customBtn)
    expect(await screen.findByPlaceholderText(/e\.g\. distributed_systems/i)).toBeInTheDocument()
  })

  // Test 13: Company ATS board discovery
  it('13. probes company URL and detects ATS board type', async () => {
    const api = mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const sourcesStepBtn = await screen.findByRole('button', { name: /7\. Job Sources/i })
    fireEvent.click(sourcesStepBtn)
    const urlInput = await screen.findByPlaceholderText(/boards\.greenhouse\.io\/stripe/i)
    fireEvent.change(urlInput, { target: { value: 'https://boards.greenhouse.io/stripe' } })
    const probeBtn = screen.getByRole('button', { name: /Discover ATS/i })
    fireEvent.click(probeBtn)
    await waitFor(() => {
      expect(api.callsTo('POST', '/api/settings/discover-company')).toHaveLength(1)
    })
    expect(await screen.findByText(/Discovered Stripe/i)).toBeInTheDocument()
  })

  // Test 14: Finish step First Ingest live runner
  it('14. triggers live first ingest on finish step with progress bar', async () => {
    const api = mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const finishStepBtn = await screen.findByRole('button', { name: /10\. Ready to Hunt/i })
    fireEvent.click(finishStepBtn)
    const ingestBtn = await screen.findByRole('button', { name: /Run Quick First Ingest/i })
    fireEvent.click(ingestBtn)
    await waitFor(() => {
      expect(api.callsTo('POST', '/api/setup/first-ingest')).toHaveLength(1)
    })
    expect(await screen.findByText(/Ollama: Offline \(Gracefully Skipped\)/i)).toBeInTheDocument()
  })

  // Test 15: Finish step First Ingest cancellation
  it('15. allows cancelling running first ingest', async () => {
    const api = mockApi(defaultWizardApi())
    renderWithProviders(<SetupWizardPage />, '/setup')
    const finishStepBtn = await screen.findByRole('button', { name: /10\. Ready to Hunt/i })
    fireEvent.click(finishStepBtn)
    const ingestBtn = await screen.findByRole('button', { name: /Run Quick First Ingest/i })
    fireEvent.click(ingestBtn)
    const cancelBtn = await screen.findByRole('button', { name: /Cancel Ingest/i })
    fireEvent.click(cancelBtn)
    await waitFor(() => {
      expect(api.callsTo('POST', '/api/setup/ingest-cancel')).toHaveLength(1)
    })
  })

  // Test 16: SettingsPage Danger Zone modal requiring MOVE DATA ASIDE
  it('16. requires exact confirmation phrase MOVE DATA ASIDE to move data', async () => {
    const api = mockApi({
      'GET /api/settings/secrets-status': () => ({ gemini_key: { present: true, last4: '1234' } }),
      'GET /api/settings/models': () => ({ live_testing_available: true, models: [] }),
      'GET /api/settings/budget': () => ({
        budget: { daily_calls: 20, interactive_daily_calls: 15, monthly_usd: 1.0, preset: 'free' },
        usage: { today_calls: 0, today_cost_usd: 0 },
        reset_time_utc: '00:00:00Z',
      }),
      'GET /api/profile/role-families/presets': () => ({ presets: [] }),
      'GET /api/profile/role-families': () => ({ active_families: [] }),
      'GET /api/resumes/bases': () => ({ bases: [] }),
      'GET /api/facts': () => ({ facts: [] }),
      'GET /api/profile/summaries': () => ({ summaries: {} }),
      'GET /api/profile/answers': () => ({ answers: {} }),
      'GET /api/settings/sources': () => ({ sources: {} }),
      'GET /api/settings/schedule': () => ({ schedule: {} }),
      'GET /api/settings/gmail': () => ({ enabled: false, token_present: false }),
      'POST /api/settings/danger/move-aside': () => ({
        status: 'ok',
        timestamp: '20261030_120000',
        trash_path: 'data/trash/20261030_120000',
      }),
    })

    renderWithProviders(<SettingsPage />, '/settings?tab=danger')
    const moveAsideBtn = await screen.findByRole('button', { name: /Move Data Aside…/i })
    fireEvent.click(moveAsideBtn)

    // Modal opens
    const phraseInput = await screen.findByPlaceholderText(/Type MOVE DATA ASIDE/i)
    expect(phraseInput).toBeInTheDocument()
    const confirmBtn = screen.getByRole('button', { name: /^Move Data Aside$/i })
    expect(confirmBtn).toBeDisabled()

    // Type incorrect phrase
    fireEvent.change(phraseInput, { target: { value: 'wrong phrase' } })
    expect(confirmBtn).toBeDisabled()

    // Type exact phrase
    fireEvent.change(phraseInput, { target: { value: 'MOVE DATA ASIDE' } })
    expect(confirmBtn).not.toBeDisabled()

    fireEvent.click(confirmBtn)
    await waitFor(() => {
      expect(api.callsTo('POST', '/api/settings/danger/move-aside')).toHaveLength(1)
    })
  })

  // Test 17: SettingsPage "Run Setup Again" resets state and redirects
  it('17. allows resetting setup wizard via Run Setup Again button', async () => {
    const api = mockApi({
      'GET /api/settings/secrets-status': () => ({ gemini_key: { present: true, last4: '1234' } }),
      'GET /api/settings/models': () => ({ live_testing_available: true, models: [] }),
      'GET /api/settings/budget': () => ({
        budget: { daily_calls: 20, interactive_daily_calls: 15, monthly_usd: 1.0, preset: 'free' },
        usage: { today_calls: 0, today_cost_usd: 0 },
        reset_time_utc: '00:00:00Z',
      }),
      'GET /api/profile/role-families/presets': () => ({ presets: [] }),
      'GET /api/profile/role-families': () => ({ active_families: [] }),
      'GET /api/resumes/bases': () => ({ bases: [] }),
      'GET /api/facts': () => ({ facts: [] }),
      'GET /api/profile/summaries': () => ({ summaries: {} }),
      'GET /api/profile/answers': () => ({ answers: {} }),
      'GET /api/settings/sources': () => ({ sources: {} }),
      'GET /api/settings/schedule': () => ({ schedule: {} }),
      'GET /api/settings/gmail': () => ({ enabled: false, token_present: false }),
      'POST /api/settings/run-setup-again': () => ({
        status: 'ok',
        message: 'Setup wizard reset to step 1 (welcome).',
      }),
    })

    renderWithProviders(<SettingsPage />, '/settings?tab=danger')
    const runAgainBtn = await screen.findByRole('button', { name: /Run Setup Again/i })
    fireEvent.click(runAgainBtn)
    await waitFor(() => {
      expect(api.callsTo('POST', '/api/settings/run-setup-again')).toHaveLength(1)
    })
  })
})
