import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { TailoringStrength } from '../api/types'
import { mockApi, renderWithProviders } from '../test/utils'
import { SkillGapsSection, StrengthChip, StrengthDetail } from './TailoringStrength'

const low: TailoringStrength = {
  level: 'Low',
  score: 0.21,
  coverage: 0.24,
  adaptation: 0.25,
  parts: { summary: true, projects: false, bullets: false, skills: false },
  missing: ['spring boot', 'jpa/hibernate'],
  why: ['all your relevant facts already fit on one page, so there was nothing less relevant to swap out', 'missing from your resume: Spring Boot, JPA/Hibernate (not on your resume, so never added)'],
  help: 'add facts for the missing items you really have (see Skill gaps on the Health page), or a base resume written for this kind of job',
  text: 'Low: 24% of the job\'s weighted concepts covered.',
}

describe('Tailoring strength', () => {
  it('shows Low with the coverage number, and why and what would help', () => {
    renderWithProviders(
      <div>
        <StrengthChip strength={low} />
        <StrengthDetail strength={low} />
      </div>,
      '/resumes',
    )
    expect(screen.getAllByText('Low').length).toBeGreaterThan(0)
    expect(screen.getAllByText('24% JD')[0]).toBeInTheDocument()
    expect(screen.getByText(/nothing less relevant to swap out/)).toBeInTheDocument()
    expect(screen.getByText(/Spring Boot, JPA\/Hibernate/)).toBeInTheDocument()
    expect(screen.getByText(/What would help: add facts for the missing items/)).toBeInTheDocument()
    expect(document.body.textContent).toContain('summary ✓ · projects – · bullets – · skills –')
  })

  it('says Unknown when the comparison was not recorded', () => {
    renderWithProviders(
      <StrengthDetail strength={{ ...low, level: 'Unknown', score: null, coverage: null, adaptation: null, parts: {}, why: [], help: null, text: 'Tailoring strength unknown: not recorded.' }} />,
      '/resumes',
    )
    expect(screen.getByText('Unknown')).toBeInTheDocument()
    expect(screen.getByText(/strength unknown: not recorded/)).toBeInTheDocument()
  })
})

describe('Skill gaps', () => {
  it('reads nothing until asked, then lists the missing concepts read-only', async () => {
    const user = userEvent.setup()
    const api = mockApi({
      'GET /api/skill-gaps': () => ({
        jobs_checked: 812, job_limit: 1200, concepts_on_your_facts: 61, note: 'Read-only.',
        items: [
          { concept: 'spring boot', label: 'Spring Boot', jobs: 74, examples: ['Java Developer (Acme)', 'Backend Engineer (Globex)'] },
          { concept: 'kubernetes', label: 'Kubernetes', jobs: 51, examples: ['Platform Engineer (Initech)'] },
        ],
      }),
    })
    renderWithProviders(<SkillGapsSection />, '/health')
    expect(api.calls).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: 'Show skill gaps' }))
    const row = (await screen.findByText('Spring Boot')).closest('tr') as HTMLElement
    expect(within(row).getByText('74')).toBeInTheDocument()
    expect(within(row).getByText(/Java Developer \(Acme\) · Backend Engineer \(Globex\)/)).toBeInTheDocument()
    expect(screen.getByText(/812 relevant open jobs checked/)).toBeInTheDocument()
    expect(api.calls.every((call) => call.method === 'GET')).toBe(true)
  })
})
