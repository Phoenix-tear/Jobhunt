import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AuditReviewState } from '../api/types'
import { mockApi, renderWithProviders } from '../test/utils'
import { TriageAuditPage } from './TriageAuditPage'

afterEach(() => {
  vi.unstubAllGlobals()
})

const mockState: AuditReviewState = {
  items: [
    {
      job_id: 101,
      company: 'Google',
      title: 'Staff Software Engineer',
      source: 'greenhouse',
      category: 'too_senior',
      snippet: 'Requires 5+ years of distributed systems experience.',
      gemini_verdict: 'senior (years=5)',
      gemini_quote: '5+ years',
      local_verdict: 'mid (years=1)',
      local_quote: 'distributed systems',
      deterministic_hint: "Snippet contains '5+ years': supports years >= 2 (senior/lead)",
      row_order: 0,
      my_verdict: null,
      my_note: null,
    },
    {
      job_id: 102,
      company: 'Meta',
      title: 'Software Engineer - US Only',
      source: 'lever',
      category: 'not_india_eligible',
      snippet: 'Must reside in the US. No international relocation offered.',
      gemini_verdict: 'india=no',
      gemini_quote: 'Must reside in the US',
      local_verdict: 'india=yes',
      local_quote: 'remote',
      deterministic_hint: "Snippet contains 'Must reside in the US': explicit US restriction (not India eligible)",
      row_order: 1,
      my_verdict: null,
      my_note: null,
    },
  ],
  current_index: 0,
  total_items: 2,
  answered_count: 0,
  gemini_correct: 0,
  local_correct: 0,
  both_wrong_count: 0,
  both_right_count: 0,
  unsure_count: 0,
  decisive_count: 0,
  gemini_accuracy: 0.0,
  gemini_ci: [0.0, 0.0],
  local_accuracy: 0.0,
  local_ci: [0.0, 0.0],
  acting_rules: [
    {
      field: 'years',
      operator: '>=',
      threshold: 2,
      target_class: 'senior',
      weight: 5.0,
      sample_size: 15,
      precision: 0.93,
    },
  ],
}

describe('TriageAuditPage', () => {
  it('renders active item details, cue highlights, model verdicts, and deterministic hint', async () => {
    mockApi({
      'GET /api/triage/audit': () => mockState,
    })

    renderWithProviders(<TriageAuditPage />, '/audit')

    expect(await screen.findByText('Triage Audit Review')).toBeInTheDocument()
    expect(screen.getByText('Row 1 of 2')).toBeInTheDocument()
    expect(screen.getByText('Google')).toBeInTheDocument()
    expect(screen.getByText('Staff Software Engineer')).toBeInTheDocument()

    // Gemini and Local LLM verdicts
    expect(screen.getByText('senior (years=5)')).toBeInTheDocument()
    expect(screen.getByText('mid (years=1)')).toBeInTheDocument()

    // Deterministic hint
    expect(screen.getByText(/Deterministic hint \(never auto-applied\):/)).toBeInTheDocument()
    expect(screen.getByText(/supports years >= 2/)).toBeInTheDocument()

    // Acting rules
    expect(screen.getByText('Acting Rules (1)')).toBeInTheDocument()
    expect(screen.getByText('years')).toBeInTheDocument()
  })

  it('clicking a verdict records vote and advances to next item', async () => {
    const item0 = mockState.items[0]!
    const item1 = mockState.items[1]!
    const updatedState: AuditReviewState = {
      ...mockState,
      answered_count: 1,
      gemini_correct: 1,
      decisive_count: 1,
      gemini_accuracy: 1.0,
      gemini_ci: [0.2, 1.0],
      current_index: 1,
      items: [
        { ...item0, my_verdict: 'gemini_right' },
        item1,
      ],
    }

    const { callsTo } = mockApi({
      'GET /api/triage/audit': () => mockState,
      'POST /api/triage/audit/label': () => updatedState,
    })

    renderWithProviders(<TriageAuditPage />, '/audit')

    expect(await screen.findByText('Staff Software Engineer')).toBeInTheDocument()

    // Click "[2] Gemini Right"
    const geminiBtn = screen.getByRole('button', { name: /Gemini Right/i })
    fireEvent.click(geminiBtn)

    await waitFor(() => {
      const calls = callsTo('POST', '/api/triage/audit/label')
      expect(calls.length).toBe(1)
      expect(calls[0]!.body).toEqual({
        job_id: 101,
        my_verdict: 'gemini_right',
        my_note: '',
      })
    })

    // Should now show item 2
    expect(await screen.findByText('Software Engineer - US Only')).toBeInTheDocument()
  })

  it('handles keyboard shortcuts for voting (1 for local_right)', async () => {
    const item0 = mockState.items[0]!
    const item1 = mockState.items[1]!
    const updatedState: AuditReviewState = {
      ...mockState,
      answered_count: 1,
      local_correct: 1,
      decisive_count: 1,
      local_accuracy: 1.0,
      local_ci: [0.2, 1.0],
      current_index: 1,
      items: [
        { ...item0, my_verdict: 'local_right' },
        item1,
      ],
    }

    const { callsTo } = mockApi({
      'GET /api/triage/audit': () => mockState,
      'POST /api/triage/audit/label': () => updatedState,
    })

    renderWithProviders(<TriageAuditPage />, '/audit')

    expect(await screen.findByText('Staff Software Engineer')).toBeInTheDocument()

    // Press '1'
    fireEvent.keyDown(window, { key: '1' })

    await waitFor(() => {
      const calls = callsTo('POST', '/api/triage/audit/label')
      expect(calls.length).toBe(1)
      expect(calls[0]!.body).toEqual({
        job_id: 101,
        my_verdict: 'local_right',
        my_note: '',
      })
    })
  })
})
