import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { JobDetail } from '../api/types'
import { makeJob, mockApi, renderWithProviders } from '../test/utils'
import { WritingSection } from './WritingSection'

const job = { ...makeJob({ id: 7 }), signals: [], eligibility_screens: [] } as unknown as JobDetail

describe('Knock-out check', () => {
  it('posts one question per line to the no-model endpoint and shows results as plain text', async () => {
    const api = mockApi({
      'GET /api/jobs/7/writing': () => [],
      'POST /api/jobs/7/knockouts': () => [
        {
          question: '<img src=x onerror=alert(1)> Minimum 8.5 CGPA',
          kind: 'cgpa_percentage',
          risk: 'possible_knockout',
          your_value: '8.1/10',
          needs_confirmation: false,
          note: 'possible knock-out: asks 8.5 CGPA (out of 10) vs your value 8.1',
        },
      ],
    })
    renderWithProviders(<WritingSection job={job} />, '/jobs?job=7')
    fireEvent.change(screen.getByLabelText(/answer a question/i), {
      target: { value: '<img src=x onerror=alert(1)> Minimum 8.5 CGPA\nNo active backlogs' },
    })
    fireEvent.click(screen.getByRole('button', { name: /check for knock-outs/i }))
    const list = await screen.findByRole('list', { name: /knock-out results/i })
    expect(within(list).getByText('possible knock-out')).toBeInTheDocument()
    expect(within(list).getByText(/vs your value 8.1/)).toBeInTheDocument()
    expect(list.querySelector('img')).toBeNull() // shown as text, never as markup
    await waitFor(() => {
      const call = api.calls.find((c) => c.url.pathname === '/api/jobs/7/knockouts')
      expect(call?.body).toEqual({ questions: ['<img src=x onerror=alert(1)> Minimum 8.5 CGPA', 'No active backlogs'] })
    })
  })
})

const letter = {
  id: 11,
  job_id: 7,
  kind: 'cover_letter',
  question: null,
  text: 'I built a portal. I built an agent. I built a bot.',
  cited: ['T-1'],
  validation: { ok: true, violations: [], words: 11 },
  words: 11,
  source: 'model',
  model: 'gemini-2.5-pro',
  input_tokens: 10,
  output_tokens: 10,
  cost_usd: 0.0345,
  message: '3 call(s): plan ok, write ok, edit ok',
  cached: false,
  version: 1,
  created_at: '2026-10-24T00:00:00Z',
  rejected_violations: [],
  pipeline: 'staged',
  stages: [
    { stage: 'plan', model: 'gemini-3.8-flash', input_tokens: 1, output_tokens: 1, cost_usd: 0.0031, accepted: true, note: '', attempt: 0 },
    { stage: 'write', model: 'gemini-2.5-pro', input_tokens: 1, output_tokens: 1, cost_usd: 0.0212, accepted: true, note: '', attempt: 1 },
    { stage: 'edit', model: 'gemini-2.5-pro', input_tokens: 1, output_tokens: 1, cost_usd: 0.0102, accepted: false, note: 'the edit changed the facts', attempt: 1 },
  ],
  plan: {},
  quality: { ok: false, warnings: [{ rule: 'repeated_sentence_starter', detail: '3 sentences in a row start with "i"' }], stats: {} },
  parent_id: null,
  used_for_export: false,
  calls: 3,
}

describe('Cover letter editor', () => {
  it('shows the text in an editor with the quality badge, warnings and a cost per stage', async () => {
    mockApi({ 'GET /api/jobs/7/writing': () => [letter] })
    renderWithProviders(<WritingSection job={job} />, '/jobs?job=7')
    const box = await screen.findByLabelText('Cover letter text')
    expect(box).toHaveValue(letter.text)
    expect(screen.getByText('facts validated ✓')).toBeInTheDocument()
    expect(screen.getByText('1 language warning')).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Language warnings' })).toHaveTextContent('repeated sentence starter')
    const costs = screen.getByTestId('stage-costs')
    expect(costs).toHaveTextContent('plan gemini-3.8-flash $0.0031')
    expect(costs).toHaveTextContent('write gemini-2.5-pro $0.0212')
    expect(costs).toHaveTextContent('edit gemini-2.5-pro $0.0102 (not used)')
    expect(costs).toHaveTextContent('total $0.0345')
    expect(screen.getByText(/12 words/)).toBeInTheDocument()
  })

  it('saves my edit as a new version without a model call, and updates the word count while typing', async () => {
    const saved = { ...letter, id: 12, version: 2, source: 'user_edit', stages: [], cost_usd: 0, used_for_export: true, parent_id: 11 }
    const api = mockApi({
      'GET /api/jobs/7/writing': () => [letter],
      'POST /api/writing/11/edit': () => saved,
    })
    renderWithProviders(<WritingSection job={job} />, '/jobs?job=7')
    const box = await screen.findByLabelText('Cover letter text')
    const save = screen.getByRole('button', { name: 'Save my edit' })
    expect(save).toBeDisabled()
    fireEvent.change(box, { target: { value: 'I built a portal for students and shipped it.' } })
    expect(screen.getByText(/9 words/)).toBeInTheDocument()
    expect(screen.getByText(/unsaved changes/)).toBeInTheDocument()
    fireEvent.click(save)
    await waitFor(() => expect(api.callsTo('POST', '/api/writing/11/edit')).toHaveLength(1))
    expect(api.lastCall('POST', '/api/writing/11/edit')?.body).toEqual({ text: 'I built a portal for students and shipped it.' })
    expect(api.callsTo('POST', '/api/jobs/7/cover-letter')).toHaveLength(0)
  })

  it('regenerates only after showing the estimate and the interactive counter', async () => {
    const api = mockApi({
      'GET /api/jobs/7/writing': () => [letter],
      'GET /api/jobs/7/writing/estimate': () => ({
        calls: 3, max_calls: 5, estimated_usd: 0.041, max_usd: 0.083, interactive_used: 12, interactive_limit: 40,
        interactive_remaining: 28, blocked_reason: '', model: 'gemini-2.5-pro', plan_model: 'gemini-3.8-flash',
        pipeline: 'staged', voice_samples: false,
      }),
      'POST /api/jobs/7/cover-letter': () => ({ ...letter, id: 13, version: 2 }),
    })
    renderWithProviders(<WritingSection job={job} />, '/jobs?job=7')
    fireEvent.click(await screen.findByRole('button', { name: 'Regenerate…' }))
    const estimate = await screen.findByTestId('estimate')
    expect(estimate).toHaveTextContent('About 3 calls (up to 5)')
    expect(estimate).toHaveTextContent('Interactive calls today: 12/40')
    expect(api.callsTo('POST', '/api/jobs/7/cover-letter')).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate (new calls)' }))
    await waitFor(() => expect(api.lastCall('POST', '/api/jobs/7/cover-letter')?.body).toEqual({ regenerate: true }))
  })

  it('the first letter also waits for the estimate confirmation', async () => {
    const api = mockApi({
      'GET /api/jobs/7/writing': () => [],
      'GET /api/jobs/7/writing/estimate': () => ({
        calls: 3, max_calls: 5, estimated_usd: 0.04, max_usd: 0.08, interactive_used: 0, interactive_limit: 40,
        interactive_remaining: 40, blocked_reason: 'interactive limit reached today (40/40), resets 05:30 IST', model: 'm',
        plan_model: 'p', pipeline: 'staged', voice_samples: true,
      }),
    })
    renderWithProviders(<WritingSection job={job} />, '/jobs?job=7')
    fireEvent.click(await screen.findByRole('button', { name: 'Cover letter' }))
    expect(await screen.findByTestId('estimate')).toHaveTextContent('Blocked: interactive limit reached today (40/40)')
    expect(api.callsTo('POST', '/api/jobs/7/cover-letter')).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: 'Cover letter' })).toBeInTheDocument()
  })
})

describe('Outline and copy warnings (v8)', () => {
  const outline = {
    ...letter,
    id: 21,
    source: 'outline',
    text: 'Facts outline for this job (not a cover letter)\n- [Proj] Built a thing',
    stages: [],
    quality: {},
    message: '3 call(s): plan ok; no letter was produced: every draft was rejected by the checks (it used the number 91, which is not in the evidence: remove it); x',
  }

  it('labels an outline as not a letter, shows the reason, and offers no export', async () => {
    mockApi({ 'GET /api/jobs/7/writing': () => [outline] })
    renderWithProviders(<WritingSection job={job} />, '/jobs?job=7')
    expect(await screen.findByText('outline (not a letter)')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('No letter was produced: every draft was rejected by the checks')
    expect(screen.getByRole('alert')).toHaveTextContent('not a cover letter')
    expect(screen.queryByText('.pdf')).toBeNull()
    expect(screen.queryByText('.txt')).toBeNull()
    expect(screen.getByLabelText('Cover letter text')).toBeDisabled()
  })

  it('labels an old template row the same way', async () => {
    mockApi({ 'GET /api/jobs/7/writing': () => [{ ...outline, source: 'template', message: '' }] })
    renderWithProviders(<WritingSection job={job} />, '/jobs?job=7')
    expect(await screen.findByText('outline (not a letter)')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('older automatic text')
  })

  it('shows copy-detection warnings next to the text', async () => {
    const edited = {
      ...letter,
      id: 22,
      source: 'user_edit',
      validation: { ok: false, violations: [{ rule: 'copied_run_from_facts', offending: 'I built a tool-using agent...', source: 'x' }] },
    }
    mockApi({ 'GET /api/jobs/7/writing': () => [edited] })
    renderWithProviders(<WritingSection job={job} />, '/jobs?job=7')
    const list = await screen.findByRole('list', { name: 'Copy and validator warnings' })
    expect(list).toHaveTextContent('copied run from facts: I built a tool-using agent')
  })
})
