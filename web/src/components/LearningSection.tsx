import { useLearning, useLearningActions } from '../api/hooks'
import { formatDate } from '../lib/format'
import { useToast } from './Toast'
import { Chip, ErrorState, Spinner } from './ui'

const LABEL_NAMES: Record<string, string> = {
  applied: 'Applied (strong positive)',
  saved: 'Saved (positive)',
  not_applying_role_fit: 'Not applying: wrong role / too senior / wrong domain (role-fit negative)',
  not_applying_other_reasons: 'Not applying: other reasons (not used as role-fit labels)',
  older_like_dismiss_clicks: 'Older like / dismiss clicks without a decision',
}

const button = 'rounded-md border border-stone-300 px-2.5 py-1 text-xs font-medium hover:bg-stone-100 disabled:opacity-50 dark:border-stone-600 dark:hover:bg-stone-800'

/** What the system has learned from my decisions, the ranker's state, and the rules I can approve, reject or remove. */
export function LearningSection() {
  const learning = useLearning()
  const actions = useLearningActions()
  const { notify } = useToast()
  if (learning.isPending) return <Spinner label="Loading what was learned" />
  if (learning.isError) return <ErrorState message={learning.error.message} onRetry={() => void learning.refetch()} />
  const data = learning.data
  const ranker = data.ranker
  const decision = ranker.last_decision
  return (
    <section aria-label="Learning" className="rounded-lg border border-stone-200 bg-white px-4 py-3 text-sm dark:border-stone-800 dark:bg-stone-900">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-stone-500">Learning from your decisions</h2>
      <p className="mb-2 text-xs text-stone-600 dark:text-stone-300" data-testid="learning-honesty">
        You have made <strong>{data.human_labels}</strong> role-fit decisions so far. {data.honesty}
      </p>
      <ul className="mb-3 grid gap-x-6 gap-y-0.5 text-xs md:grid-cols-2">
        {Object.entries(data.labels).map(([key, value]) => (
          <li key={key} className="flex justify-between gap-2">
            <span>{LABEL_NAMES[key] ?? key}</span>
            <span className="tabular-nums">{value}</span>
          </li>
        ))}
      </ul>
      <p className="mb-3 text-xs" data-testid="ranker-state">
        Ranker: trained {ranker.trained_at ? formatDate(ranker.trained_at) : 'never'}
        {ranker.rows ? ` on ${ranker.rows} labels` : ''}
        {ranker.cv_spearman !== null ? `, cross-validated Spearman ${ranker.cv_spearman} (raw similarity alone: ${ranker.similarity_only_spearman ?? 'n/a'})` : ''}
        {ranker.in_use ? ` · model in use: ${ranker.in_use}` : ''}.
        {decision ? ` Last check: ${decision.action.replace('_', ' ')}${decision.reason ? ` (${decision.reason})` : ''}.` : ''}
      </p>

      <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-stone-500">Suggested rules</h3>
      {data.proposals.length === 0 ? (
        <p className="mb-3 text-xs text-stone-500">Nothing to suggest yet. A suggestion needs evidence from at least 5 of your decisions.</p>
      ) : (
        <ul className="mb-3 space-y-2" aria-label="Suggested rules">
          {data.proposals.map((proposal) => (
            <li key={proposal.key} className="rounded border border-stone-200 p-2 text-xs dark:border-stone-700">
              <p>
                <strong>{proposal.kind === 'title_phrase' ? 'Title word' : 'Eligibility phrase'}: “{proposal.phrase}”</strong>{' '}
                <span className="text-stone-500">
                  {proposal.count} of your decisions ({proposal.reason}); {proposal.kept} similar jobs you did not mark; jobs {proposal.job_ids.slice(0, 8).join(', ')}
                </span>
              </p>
              <p className="text-stone-500">If approved it can only move such jobs from target to maybe and subtract a few score points; it never excludes. {proposal.examples.join(' · ')}</p>
              <div className="mt-1 flex gap-2">
                <button
                  type="button"
                  className={button}
                  disabled={actions.approve.isPending}
                  onClick={() =>
                    actions.approve.mutate(proposal.key, {
                      onSuccess: () => notify(`Rule approved: “${proposal.phrase}”. It applies at the next relevance run.`, 'info'),
                      onError: (error) => notify(`Could not approve: ${error.message}`, 'error'),
                    })
                  }
                >
                  Approve
                </button>
                <button type="button" className={button} disabled={actions.reject.isPending} onClick={() => actions.reject.mutate(proposal.key)}>
                  Reject
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-stone-500">Active learned rules</h3>
      {data.rules.length === 0 ? (
        <p className="text-xs text-stone-500">None. {data.applies}</p>
      ) : (
        <ul className="space-y-1" aria-label="Active learned rules">
          {data.rules.map((rule) => (
            <li key={rule.id} className="flex flex-wrap items-center gap-2 text-xs">
              <Chip>{rule.id}</Chip>
              <span>
                “{rule.phrase}” ({rule.kind === 'title_phrase' ? 'title' : 'posting text'}; {rule.demote ? 'target to maybe' : ''}
                {rule.penalty ? `, -${rule.penalty} points` : ''}; evidence {rule.count} jobs, approved {rule.created})
              </span>
              <button type="button" className={button} disabled={actions.remove.isPending} onClick={() => actions.remove.mutate(rule.id)}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
