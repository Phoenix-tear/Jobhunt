import type { ScheduleHealth } from '../api/types'
import { formatDate, timeAgo } from '../lib/format'

function statusTone(status: string | null): string {
  if (status === 'ok') return 'text-emerald-700 dark:text-emerald-300'
  if (status === 'failed' || status === 'timeout') return 'text-rose-700 dark:text-rose-300'
  return 'text-stone-500'
}

/** Scheduled tasks: last run, status, duration, next run, and a red warning when more than 2 runs were missed. */
export function ScheduleSection({ schedule }: { schedule: ScheduleHealth }) {
  const backendName = schedule.backend || 'windows_task_scheduler'
  const missedList = schedule.missed_runs || []

  return (
    <section aria-label="Scheduled tasks" className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-stone-500">Scheduled tasks</h2>
        <span className="rounded bg-stone-100 px-2 py-0.5 text-xs text-stone-600 dark:bg-stone-800 dark:text-stone-300">
          Backend: <code className="font-mono">{backendName}</code>
        </span>
      </div>
      {missedList.length > 0 ? (
        <div role="alert" className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/50 dark:text-amber-100">
          <p className="font-medium">Missed runs detected ({missedList.length}):</p>
          <ul className="mt-1 list-inside list-disc text-xs">
            {missedList.map((m) => (
              <li key={m.task}>{m.name}: {m.missed_runs} missed run(s)</li>
            ))}
          </ul>
        </div>
      ) : null}
      {schedule.gmail_auth_expired ? (
        <div role="alert" className="rounded-lg border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-900 dark:border-rose-800 dark:bg-rose-950/50 dark:text-rose-100">
          <p className="font-medium">Gmail authorization problem: e-mail ingest is being skipped.</p>
          {schedule.gmail_auth_reason ? <p className="mt-1 text-xs">Reason: {schedule.gmail_auth_reason}</p> : null}
          <p className="mt-1">
            Re-authorize with: <code className="rounded bg-white/70 px-1 dark:bg-black/30">{schedule.gmail_reauth_command}</code>
          </p>
        </div>
      ) : null}
      <div className="overflow-x-auto rounded-lg border border-stone-200 bg-white px-4 dark:border-stone-800 dark:bg-stone-900">
        <table className="w-full min-w-[760px] text-left text-sm">
          <thead>
            <tr className="text-xs uppercase tracking-wide text-stone-500">
              <th className="py-2 pr-3 font-medium">Task</th>
              <th className="py-2 pr-3 font-medium">Last run</th>
              <th className="py-2 pr-3 font-medium">Status</th>
              <th className="py-2 pr-3 font-medium">Duration</th>
              <th className="py-2 pr-3 font-medium">Next run</th>
              <th className="py-2 font-medium">Missed</th>
            </tr>
          </thead>
          <tbody>
            {schedule.tasks.map((task) => (
              <tr key={task.task} className="border-t border-stone-100 align-top dark:border-stone-800" data-missed={task.missed_warning}>
                <td className="py-2 pr-3">
                  <span className="font-medium">{task.name}</span>
                  <span className="block text-xs text-stone-500">{task.schedule}</span>
                </td>
                <td className="py-2 pr-3">{task.last_run_at ? timeAgo(task.last_run_at) : 'never'}</td>
                <td className={`py-2 pr-3 ${statusTone(task.last_status)}`}>
                  {task.last_status ?? '-'}
                  {task.last_detail ? <span className="block text-xs text-stone-500">{task.last_detail}</span> : null}
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {task.last_duration_seconds !== null ? `${Math.round(task.last_duration_seconds)} s` : '-'}
                </td>
                <td className="py-2 pr-3">{task.next_run_at ? formatDate(task.next_run_at) + ' ' + task.next_run_at.slice(11, 16) + ' UTC' : '-'}</td>
                <td className="py-2">
                  {task.missed_warning ? (
                    <span role="alert" className="rounded bg-rose-100 px-1.5 py-0.5 text-xs font-semibold text-rose-900 dark:bg-rose-900/60 dark:text-rose-100">
                      missed {task.missed_runs}
                    </span>
                  ) : (
                    <span className="text-xs text-stone-500">{task.missed_runs}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
