import type { FreshnessHealth } from '../api/types'
import { sourceLabel, timeAgo } from '../lib/format'
import { Chip } from './ui'

const BAND_LABELS: Record<string, string> = { fresh: 'fresh', aging: 'aging', unknown: 'age unknown', stale: 'stale' }

/** Health: age bands, what was auto-hidden (per source) and the per-source "closed / too old" reports with proposals. */
export function FreshnessSection({ data }: { data: FreshnessHealth }) {
  const proposals = data.source_reports.filter((report) => report.proposal)
  return (
    <section aria-label="Freshness" className="rounded-lg border border-stone-200 bg-white px-4 py-3 text-sm dark:border-stone-800 dark:bg-stone-900">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-stone-500">Freshness and closed jobs</h2>
      <p className="mb-2 text-xs text-stone-600 dark:text-stone-300">
        Fresh = posted {data.fresh_days} days ago or less, stale = older than {data.stale_after_days} days (config <code>freshness:</code>). Stale and closed jobs
        move to the <strong>Auto-hidden</strong> tab with a one-click Restore; nothing is deleted.
        {data.last_check ? ` Last "still open?" check: ${timeAgo(data.last_check)}.` : ' No "still open?" check has run yet.'}
      </p>
      <p className="mb-2 text-xs" data-testid="auto-hidden-line">
        <strong className="tabular-nums">{data.auto_hidden_total.toLocaleString()}</strong> auto-hidden
        {data.auto_hidden_by_source.length > 0 ? (
          <>
            {' '}
            by source:{' '}
            {data.auto_hidden_by_source.map((item, index) => (
              <span key={item.value}>
                {index > 0 ? ', ' : ''}
                {sourceLabel(item.value)} <span className="tabular-nums">{item.count.toLocaleString()}</span>
              </span>
            ))}
            .
          </>
        ) : null}
        {data.auto_hidden_by_reason.length > 0
          ? ` (${data.auto_hidden_by_reason.map((item) => `${item.value === 'auto_stale' ? 'too old' : 'closed'}: ${item.count.toLocaleString()}`).join(', ')})`
          : ''}
      </p>
      {data.bands.length > 0 ? (
        <p className="mb-2 flex flex-wrap items-center gap-1.5 text-xs">
          Open jobs by age:
          {data.bands.map((band) => (
            <Chip key={band.value}>
              {BAND_LABELS[band.value] ?? band.value} {band.count.toLocaleString()}
            </Chip>
          ))}
        </p>
      ) : null}

      <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-stone-500">Closed / too old reports per source</h3>
      {data.source_reports.length === 0 ? (
        <p className="text-xs text-stone-500">No reports yet. Use “Not applying → Closed or no longer available / Too old” on a job.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs" aria-label="Source reports">
            <thead className="text-stone-500">
              <tr>
                <th className="py-1 pr-3 font-medium">Source</th>
                <th className="py-1 pr-3 font-medium">Closed</th>
                <th className="py-1 pr-3 font-medium">Too old</th>
                <th className="py-1 pr-3 font-medium">Shown</th>
                <th className="py-1 pr-3 font-medium">Auto-hidden</th>
                <th className="py-1 pr-3 font-medium">Stale after</th>
              </tr>
            </thead>
            <tbody>
              {data.source_reports.map((report) => (
                <tr key={report.source} className="border-t border-stone-200 dark:border-stone-800" data-source-report={report.source}>
                  <th scope="row" className="py-1 pr-3 font-medium">
                    {sourceLabel(report.source)}
                  </th>
                  <td className="py-1 pr-3 tabular-nums">{report.reported_closed}</td>
                  <td className="py-1 pr-3 tabular-nums">{report.reported_too_old}</td>
                  <td className="py-1 pr-3 tabular-nums">{report.shown.toLocaleString()}</td>
                  <td className="py-1 pr-3 tabular-nums">{report.auto_hidden.toLocaleString()}</td>
                  <td className="py-1 pr-3 tabular-nums">{report.current_stale_after_days} days</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {proposals.length > 0 ? (
        <ul aria-label="Proposals" className="mt-2 space-y-1 text-xs text-amber-900 dark:text-amber-200">
          {proposals.map((report) => (
            <li key={report.source}>Proposal: {report.proposal}</li>
          ))}
        </ul>
      ) : null}
    </section>
  )
}
