import { describe, expect, it, vi } from 'vitest'
import { jobsQueryString } from '../api/client'
import { DEFAULT_FILTERS, STORAGE_KEY, hasActiveFilters, parseFilters, recallFilters, rememberFilters, serializeFilters, toggleValue, urlHasControls } from './filters'

describe('URL <-> filter state', () => {
  it('defaults serialize to an empty query string', () => {
    expect(serializeFilters(DEFAULT_FILTERS).toString()).toBe('')
    expect(parseFilters(new URLSearchParams(''))).toEqual(DEFAULT_FILTERS)
  })

  it('round-trips every filter', () => {
    const filters = {
      q: 'machine learning',
      levels: ['intern', 'new_grad'],
      types: ['internship'],
      countries: ['IN', 'unknown'],
      sources: ['greenhouse', 'email_linkedin'],
      remote: true,
      workplace: ['hybrid'],
      remoteIndia: true,
      category: 'filtered_out' as const,
      freshness: ['fresh' as const, 'unknown' as const],
      includeDismissed: true,
      juniorOnly: true,
      locationFits: ['india', 'remote_unclear'],
      otherLocations: true,
      newWithin24h: true,
      needsDescription: true,
      hideSenior: false,
      view: 'not_applying' as const,
      sort: 'company' as const,
      page: 3,
    }
    expect(parseFilters(serializeFilters(filters))).toEqual(filters)
  })

  it('ignores garbage values instead of breaking', () => {
    const parsed = parseFilters(new URLSearchParams('sort=bogus&page=-4&hideSenior=maybe&remote=yes'))
    expect(parsed.sort).toBe('match')
    expect(parsed.page).toBe(1)
    expect(parsed.hideSenior).toBe(true)
    expect(parsed.remote).toBe(false)
    expect(parseFilters(new URLSearchParams('page=abc')).page).toBe(1)
  })

  it('maps filters to the API query', () => {
    const query = new URLSearchParams(
      jobsQueryString(
        { ...DEFAULT_FILTERS, q: ' python ', levels: ['intern'], types: ['internship'], countries: ['IN'], remote: true, newWithin24h: true, hideSenior: false, sort: 'title', page: 2 },
        25,
      ),
    )
    expect(Object.fromEntries(query)).toEqual({
      q: 'python',
      level: 'intern',
      employment_type: 'internship',
      country: 'IN',
      remote: 'true',
      new_within_hours: '24',
      hide_senior: 'false',
      category: 'relevant',
      sort: 'title',
      page: '2',
      page_size: '25',
    })
    expect(jobsQueryString(DEFAULT_FILTERS, 25)).toBe('category=relevant&sort=match&page_size=25')
  })

  it('toggleValue and hasActiveFilters', () => {
    expect(toggleValue(['a'], 'b')).toEqual(['a', 'b'])
    expect(toggleValue(['a', 'b'], 'a')).toEqual(['b'])
    expect(hasActiveFilters(DEFAULT_FILTERS)).toBe(false)
    expect(hasActiveFilters({ ...DEFAULT_FILTERS, hideSenior: false })).toBe(true)
    expect(hasActiveFilters({ ...DEFAULT_FILTERS, sort: 'company', page: 4 })).toBe(false)
  })
})

describe('category, sort and remembered controls', () => {
  it('maps links from before the category control', () => {
    expect(parseFilters(new URLSearchParams('filtered=1')).category).toBe('filtered_out')
    expect(parseFilters(new URLSearchParams('relevant=0')).category).toBe('all')
    expect(parseFilters(new URLSearchParams('category=maybe')).category).toBe('maybe')
    expect(parseFilters(new URLSearchParams('category=bogus')).category).toBe('relevant')
  })

  it('every sort key round-trips through the URL and a bad one falls back to Best match', () => {
    for (const sort of ['match', 'newest_posted', 'newest_seen', 'oldest', 'company', 'title'] as const) {
      expect(parseFilters(serializeFilters({ ...DEFAULT_FILTERS, sort })).sort).toBe(sort)
    }
    expect(parseFilters(new URLSearchParams('sort=newest;drop')).sort).toBe('match')
  })

  it('sort and category are independent in the URL', () => {
    const params = serializeFilters({ ...DEFAULT_FILTERS, category: 'target', sort: 'oldest', freshness: ['fresh'] })
    expect(params.toString()).toBe('category=target&fresh=fresh&sort=oldest')
    const parsed = parseFilters(params)
    expect([parsed.category, parsed.sort, parsed.freshness]).toEqual(['target', 'oldest', ['fresh']])
    expect(hasActiveFilters({ ...DEFAULT_FILTERS, sort: 'oldest' })).toBe(false)
    expect(hasActiveFilters({ ...DEFAULT_FILTERS, category: 'maybe' })).toBe(true)
  })

  it('remembers and recalls the five controls, ignoring garbage and blocked storage', () => {
    rememberFilters({ ...DEFAULT_FILTERS, category: 'maybe', view: 'saved', sort: 'company', locationFits: ['india'], freshness: ['aging'] })
    expect(recallFilters()).toEqual({ category: 'maybe', view: 'saved', sort: 'company', locationFits: ['india'], freshness: ['aging'] })
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ category: 'nope', sort: 'x', view: 'y', locationFits: 'z', freshness: ['fresh', 'bad'] }))
    expect(recallFilters()).toEqual({ freshness: ['fresh'] })
    window.localStorage.setItem(STORAGE_KEY, '{broken')
    expect(recallFilters()).toBeNull()
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(recallFilters()).toBeNull()
    expect(() => rememberFilters(DEFAULT_FILTERS)).not.toThrow()
    vi.restoreAllMocks()
  })

  it('urlHasControls only looks at the five controls', () => {
    expect(urlHasControls(new URLSearchParams('q=python&level=intern'))).toBe(false)
    expect(urlHasControls(new URLSearchParams('sort=title'))).toBe(true)
    expect(urlHasControls(new URLSearchParams('category=all'))).toBe(true)
  })
})
