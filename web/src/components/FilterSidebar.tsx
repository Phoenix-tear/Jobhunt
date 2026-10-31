import { useState } from 'react'
import { EMPLOYMENT_TYPES, EXPERIENCE_LEVELS } from '../api/types'
import type { FacetCount, Facets } from '../api/types'
import type { Category } from '../api/types'
import type { FreshnessChoice, JobFilters } from '../lib/filters'
import { toggleValue } from '../lib/filters'
import { LEVEL_LABELS, TYPE_LABELS, countryLabel, sourceLabel } from '../lib/format'

interface Props {
  filters: JobFilters
  facets: Facets | undefined
  onChange: (patch: Partial<JobFilters>) => void
  onClear: () => void
  canClear: boolean
}

const COUNTRY_PREVIEW = 8

const CATEGORY_OPTIONS: { value: Category; label: string; hint: string }[] = [
  { value: 'relevant', label: 'Relevant', hint: 'target + maybe, inside my location policy' },
  { value: 'target', label: 'Target only', hint: 'strong title matches' },
  { value: 'maybe', label: 'Maybe', hint: 'plausible, needs a look' },
  { value: 'all', label: 'All', hint: 'every job, even the ones the gate filtered out' },
  { value: 'filtered_out', label: 'Filtered out', hint: 'only what the gate excluded, with the reason' },
]
const FRESHNESS_OPTIONS: { value: FreshnessChoice; label: string }[] = [
  { value: 'fresh', label: 'Fresh (30 days or less)' },
  { value: 'aging', label: 'Aging (31 to 45 days)' },
  { value: 'unknown', label: 'Age unknown' },
]

function countOf(items: FacetCount[] | undefined, value: string): number | null {
  const found = items?.find((item) => item.value === value)
  return found ? found.count : items ? 0 : null
}

function CheckGroup({
  legend,
  options,
  selected,
  onToggle,
}: {
  legend: string
  options: { value: string; label: string; count: number | null }[]
  selected: string[]
  onToggle: (value: string) => void
}) {
  return (
    <fieldset className="space-y-1">
      <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-stone-500 dark:text-stone-400">{legend}</legend>
      {options.map((option) => (
        <label key={option.value} className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 text-sm hover:bg-stone-100 dark:hover:bg-stone-800">
          <input
            type="checkbox"
            className="size-4 accent-brand-700"
            checked={selected.includes(option.value)}
            onChange={() => onToggle(option.value)}
          />
          <span className="flex-1 truncate">{option.label}</span>
          {option.count !== null ? <span className="text-xs tabular-nums text-stone-400">{option.count.toLocaleString()}</span> : null}
        </label>
      ))}
    </fieldset>
  )
}

function Toggle({ label, checked, onChange, hint }: { label: string; checked: boolean; onChange: (value: boolean) => void; hint?: string }) {
  return (
    <label className="flex cursor-pointer items-start gap-2 rounded px-1 py-0.5 text-sm hover:bg-stone-100 dark:hover:bg-stone-800">
      <input type="checkbox" className="mt-0.5 size-4 accent-brand-700" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      <span>
        {label}
        {hint ? <span className="ml-1 text-xs tabular-nums text-stone-400">{hint}</span> : null}
      </span>
    </label>
  )
}

export function FilterSidebar({ filters, facets, onChange, onClear, canClear }: Props) {
  const [showAllCountries, setShowAllCountries] = useState(false)

  const countries = facets?.countries ?? []
  const selectedCountries = new Set(filters.countries)
  const visibleCountries = showAllCountries
    ? countries
    : countries.filter((country, index) => index < COUNTRY_PREVIEW || selectedCountries.has(country.value))

  return (
    <form aria-label="Filters" className="space-y-5" onSubmit={(event) => event.preventDefault()}>
      <fieldset className="space-y-1" aria-label="Category">
        <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-stone-500 dark:text-stone-400">Category</legend>
        {CATEGORY_OPTIONS.map((option) => (
          <label key={option.value} title={option.hint} className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 text-sm hover:bg-stone-100 dark:hover:bg-stone-800">
            <input
              type="radio"
              name="category"
              className="size-4 accent-brand-700"
              checked={filters.category === option.value}
              onChange={() => onChange({ category: option.value })}
            />
            <span className="flex-1">{option.label}</span>
            {facets?.category_counts ? (
              <span className="text-xs tabular-nums text-stone-400" data-testid={`category-count-${option.value}`}>
                {(countOf(facets.category_counts, option.value) ?? 0).toLocaleString()}
              </span>
            ) : null}
          </label>
        ))}
      </fieldset>

      <fieldset className="space-y-1" aria-label="Freshness">
        <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-stone-500 dark:text-stone-400">Freshness</legend>
        {FRESHNESS_OPTIONS.map((option) => (
          <Toggle
            key={option.value}
            label={option.label}
            checked={filters.freshness.includes(option.value)}
            onChange={() => onChange({ freshness: toggleValue(filters.freshness, option.value) as FreshnessChoice[] })}
            hint={facets?.freshness_bands ? String(countOf(facets.freshness_bands, option.value) ?? 0) : undefined}
          />
        ))}
        <p className="px-1 text-[11px] text-stone-400">Stale jobs move to the Auto-hidden tab.</p>
      </fieldset>

      <div className="space-y-1">
        <Toggle label="Explicit junior only" checked={filters.juniorOnly} onChange={(value) => onChange({ juniorOnly: value })} />
        <Toggle label="Show dismissed" checked={filters.includeDismissed} onChange={(value) => onChange({ includeDismissed: value })} />
      </div>

      <fieldset className="space-y-1">
        <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-stone-500 dark:text-stone-400">Location</legend>
        {(
          [
            ['india', 'India'],
            ['remote_open_to_india', 'Remote, open to India'],
            ['remote_unclear', 'Remote, check scope'],
          ] as const
        ).map(([value, label]) => (
          <Toggle
            key={value}
            label={label}
            checked={filters.locationFits.includes(value)}
            onChange={() => onChange({ locationFits: toggleValue(filters.locationFits, value) })}
            hint={facets ? String(countOf(facets.location_fits, value) ?? 0) : undefined}
          />
        ))}
        <Toggle
          label="Show other locations"
          checked={filters.otherLocations ?? facets?.show_other_locations_default ?? false}
          onChange={(value) => onChange({ otherLocations: value })}
          hint={
            facets
              ? String((countOf(facets.location_fits, 'wrong_location') ?? 0) + (countOf(facets.location_fits, 'abroad_unknown_workplace') ?? 0))
              : undefined
          }
        />
      </fieldset>

      <fieldset className="space-y-1">
        <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-stone-500 dark:text-stone-400">Remote &amp; workplace</legend>
        <Toggle label="Remote only" checked={filters.remote} onChange={(value) => onChange({ remote: value })} hint={facets ? facets.remote.toLocaleString() : undefined} />
        <Toggle
          label="Remote open to India"
          checked={filters.remoteIndia}
          onChange={(value) => onChange({ remoteIndia: value })}
          hint={facets ? facets.remote_open_to_india.toLocaleString() : undefined}
        />
        <Toggle
          label="Hybrid"
          checked={filters.workplace.includes('hybrid')}
          onChange={() => onChange({ workplace: toggleValue(filters.workplace, 'hybrid') })}
          hint={facets ? String(countOf(facets.workplace_types, 'hybrid') ?? 0) : undefined}
        />
        <Toggle
          label="On-site"
          checked={filters.workplace.includes('onsite')}
          onChange={() => onChange({ workplace: toggleValue(filters.workplace, 'onsite') })}
          hint={facets ? String(countOf(facets.workplace_types, 'onsite') ?? 0) : undefined}
        />
      </fieldset>

      <div className="space-y-1">
        <Toggle label="Hide senior roles" checked={filters.hideSenior} onChange={(value) => onChange({ hideSenior: value })} />
        <Toggle
          label="New in last 24h"
          checked={filters.newWithin24h}
          onChange={(value) => onChange({ newWithin24h: value })}
          hint={facets ? facets.new_24h.toLocaleString() : undefined}
        />
        <Toggle
          label="Needs description"
          checked={filters.needsDescription}
          onChange={(value) => onChange({ needsDescription: value })}
          hint={facets ? facets.needs_description.toLocaleString() : undefined}
        />
      </div>

      <CheckGroup
        legend="Level"
        selected={filters.levels}
        onToggle={(value) => onChange({ levels: toggleValue(filters.levels, value) })}
        options={EXPERIENCE_LEVELS.map((level) => ({
          value: level,
          label: LEVEL_LABELS[level] ?? level,
          count: countOf(facets?.levels, level),
        }))}
      />

      <CheckGroup
        legend="Employment type"
        selected={filters.types}
        onToggle={(value) => onChange({ types: toggleValue(filters.types, value) })}
        options={EMPLOYMENT_TYPES.map((type) => ({
          value: type,
          label: TYPE_LABELS[type] ?? type,
          count: countOf(facets?.employment_types, type),
        }))}
      />

      <div>
        <CheckGroup
          legend="Country"
          selected={filters.countries}
          onToggle={(value) => onChange({ countries: toggleValue(filters.countries, value) })}
          options={visibleCountries.map((country) => ({
            value: country.value,
            label: countryLabel(country.value),
            count: country.count,
          }))}
        />
        {countries.length > COUNTRY_PREVIEW ? (
          <button type="button" className="mt-1 px-1 text-xs font-medium text-brand-700 hover:underline dark:text-brand-100" onClick={() => setShowAllCountries((value) => !value)}>
            {showAllCountries ? 'Show fewer' : `Show all ${countries.length}`}
          </button>
        ) : null}
      </div>

      <CheckGroup
        legend="Source"
        selected={filters.sources}
        onToggle={(value) => onChange({ sources: toggleValue(filters.sources, value) })}
        options={(facets?.sources ?? []).map((source) => ({ value: source.value, label: sourceLabel(source.value), count: source.count }))}
      />

      <button
        type="button"
        onClick={onClear}
        disabled={!canClear}
        className="w-full rounded-md border border-stone-300 px-3 py-1.5 text-sm font-medium enabled:hover:bg-stone-200 disabled:cursor-not-allowed disabled:opacity-50 dark:border-stone-700 dark:enabled:hover:bg-stone-800"
      >
        Clear filters
      </button>
    </form>
  )
}
