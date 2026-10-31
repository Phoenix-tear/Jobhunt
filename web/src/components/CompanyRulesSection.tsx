import { useState } from 'react'
import { useCompanyRuleActions, useCompanyRules } from '../api/hooks'
import type { CompanyRule } from '../api/types'
import { useToast } from './Toast'
import { ErrorState, Spinner } from './ui'

const button = 'rounded-md border border-stone-300 px-2.5 py-1 text-xs font-medium hover:bg-stone-100 disabled:opacity-50 dark:border-stone-600 dark:hover:bg-stone-800'

function List({ label, values }: { label: string; values: string[] }) {
  if (values.length === 0) return null
  return (
    <p>
      <span className="text-stone-500">{label}:</span> {values.join(', ')}
    </p>
  )
}

/** My company grade rules (data/profile/company_rules.yaml): what is open to me per employer, with Remove. */
export function CompanyRulesSection() {
  const rules = useCompanyRules()
  const { remove } = useCompanyRuleActions()
  const { notify } = useToast()
  const [confirming, setConfirming] = useState<number | null>(null)
  if (rules.isPending) return <Spinner label="Loading company rules" />
  if (rules.isError) return <ErrorState message={rules.error.message} onRetry={() => void rules.refetch()} />
  const doRemove = (rule: CompanyRule) =>
    remove.mutate(rule.index, {
      onSuccess: () => {
        setConfirming(null)
        notify(`Removed the rule for ${rule.company}. It stops applying at the next relevance run.`, 'info')
      },
      onError: (error) => notify(`Could not remove it: ${error.message}`, 'error'),
    })
  return (
    <section aria-label="Company rules" className="rounded-lg border border-stone-200 bg-white px-4 py-3 text-sm dark:border-stone-800 dark:bg-stone-900">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-stone-500">Company grade rules</h2>
      <p className="mb-2 text-xs text-stone-600 dark:text-stone-300">
        Employers that hire by grade. A denied title or grade code excludes the job; a company with allow rules where none match is at best “maybe”. Edit{' '}
        <code>{rules.data.path}</code> by hand, or use Not applying → “Ineligible: grade / level” → “Remember for this company”. Changes apply at the next relevance
        run.
      </p>
      {rules.data.rules.length === 0 ? (
        <p className="text-xs text-stone-500">No company rules.</p>
      ) : (
        <ul className="space-y-2" aria-label="Rules">
          {rules.data.rules.map((rule) => (
            <li key={rule.index} className="rounded border border-stone-200 p-2 text-xs dark:border-stone-700" data-company-rule={rule.company}>
              <p className="font-medium">{rule.company}</p>
              <List label="Also matches" values={rule.aliases} />
              <List label="Open titles / roles" values={rule.allow_title_patterns} />
              <List label="Open grade codes" values={rule.allow_level_codes} />
              <List label="Denied titles" values={rule.deny_title_patterns} />
              <List label="Denied grade codes" values={rule.deny_level_codes} />
              <List label="Denied phrases in the posting" values={rule.deny_text_patterns} />
              {rule.note ? <p className="italic text-stone-500">{rule.note}</p> : null}
              <div className="mt-1.5 flex items-center gap-2">
                {confirming === rule.index ? (
                  <>
                    <span>
                      Remove {rule.entry_indexes.length > 1 ? 'the first entry' : 'this entry'} from the file?
                    </span>
                    <button type="button" className={button} disabled={remove.isPending} onClick={() => doRemove(rule)}>
                      Yes, remove
                    </button>
                    <button type="button" className={button} onClick={() => setConfirming(null)}>
                      Keep
                    </button>
                  </>
                ) : (
                  <button type="button" className={button} onClick={() => setConfirming(rule.index)}>
                    Remove
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
