import { Link } from 'react-router-dom'
import { useBoard } from '../api/hooks'
import { AddJobForm } from '../components/AddJobForm'
import { KanbanBoard } from '../components/KanbanBoard'
import { EmptyState, ErrorState, Spinner } from '../components/ui'

export function TrackerPage() {
  const board = useBoard()

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold tracking-tight">Tracker</h1>
        <a className="text-xs text-brand-700 underline dark:text-brand-100" href="/api/applications/export.csv" download>
          Export CSV
        </a>
        <p className="text-xs text-stone-500">
          Drag a card (or use <em>Move to</em>) to change its stage. Pick up with Space on the ⋮⋮ handle, ←/→ to change column.
        </p>
      </div>

      <AddJobForm />

      {board.isPending ? <Spinner label="Loading your board" /> : null}
      {board.isError ? <ErrorState message={board.error.message} onRetry={() => void board.refetch()} /> : null}
      {board.data && board.data.total === 0 ? (
        <EmptyState title="Nothing tracked yet">
          Save a job from the <Link to="/jobs" className="underline">Jobs</Link> page, or add one by URL above.
        </EmptyState>
      ) : null}
      {board.data ? <KanbanBoard board={board.data} /> : null}
    </div>
  )
}
