import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  pointerWithin,
  rectIntersection,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type Announcements,
  type CollisionDetection,
  type DragEndEvent,
} from '@dnd-kit/core'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useDeleteApplication, usePatchApplication } from '../api/hooks'
import { requestJson } from '../api/client'
import type { TailoredResume } from '../api/types'
import type { Board, BoardCard, Stage } from '../api/types'
import { STAGE_LABELS, STAGES } from '../api/types'
import { cardId, columnId, columnKeyboardCoordinates, resolveDrop } from '../lib/kanban'
import { formatDate, isOverdue } from '../lib/format'
import { useToast } from './Toast'
import { Chip } from './ui'

const COLUMN_TONES: Record<Stage, string> = {
  saved: 'border-t-stone-400',
  applied: 'border-t-sky-500',
  oa_interview: 'border-t-amber-500',
  offer: 'border-t-emerald-500',
  rejected: 'border-t-rose-500',
}

// Pointer position wins (drop where the cursor is); keyboard drags fall back to rectangle overlap.
const collisionDetection: CollisionDetection = (args) => {
  const pointer = pointerWithin(args)
  return pointer.length > 0 ? pointer : rectIntersection(args)
}

function useMoveCard() {
  const patch = usePatchApplication()
  const { notify } = useToast()
  return (applicationId: number, stage: Stage, title: string) =>
    patch.mutate(
      { id: applicationId, patch: { stage } },
      {
        // moving to Applied: offer to attach the approved tailored resume of that job
        onSuccess: (application) => {
          if (stage !== 'applied' || application.tailored_resume_id) return
          void requestJson<TailoredResume[]>(`/api/jobs/${application.job_id}/tailored`)
            .then((versions) => {
              const approved = versions.find((version) => version.approved && version.has_pdf)
              if (!approved) return
              notify(`Attach the approved tailored resume to “${title}”?`, 'info', {
                label: 'Attach',
                onClick: () =>
                  patch.mutate(
                    { id: applicationId, patch: { tailored_resume_id: approved.id } },
                    { onError: (error) => notify(`Could not attach it: ${error.message}`, 'error') },
                  ),
              })
            })
            .catch(() => undefined)
        },
        onError: (error) =>
          notify(`Could not move “${title}” to ${STAGE_LABELS[stage]}: ${error.message}. It was put back.`, 'error'),
      },
    )
}

export function KanbanBoard({ board }: { board: Board }) {
  const move = useMoveCard()
  const [activeTitle, setActiveTitle] = useState<string | null>(null)
  const sensors = useSensors(
    // A small distance threshold keeps clicks on buttons/inputs inside a card from starting a drag.
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: columnKeyboardCoordinates }),
  )

  const cardsById = new Map<number, BoardCard>()
  for (const column of board.columns) for (const card of column.cards) cardsById.set(card.application.id, card)

  const labelFor = (id: unknown): string => {
    const raw = typeof id === 'string' ? id : ''
    if (raw.startsWith('column:')) return STAGE_LABELS[raw.slice(7) as Stage] ?? raw
    const card = cardsById.get(Number.parseInt(raw.slice(5), 10))
    return card ? `${card.title} at ${card.company_name}` : 'card'
  }

  const announcements: Announcements = {
    onDragStart: ({ active }) => `Picked up ${labelFor(active.id)}. Use the left and right arrow keys to change column, space to drop, escape to cancel.`,
    onDragOver: ({ over }) => (over ? `Over the ${labelFor(over.id)} column.` : 'Not over a column.'),
    onDragEnd: ({ active, over }) =>
      over ? `Dropped ${labelFor(active.id)} in the ${labelFor(over.id)} column.` : `${labelFor(active.id)} was dropped outside a column and stays where it was.`,
    onDragCancel: ({ active }) => `Cancelled. ${labelFor(active.id)} stays where it was.`,
  }

  const onDragEnd = (event: DragEndEvent) => {
    setActiveTitle(null)
    const current = event.active.data.current as { stage?: Stage; title?: string } | undefined
    if (!current?.stage) return
    const drop = resolveDrop(event.active.id, event.over?.id, current.stage)
    if (drop) move(drop.applicationId, drop.stage, current.title ?? 'job')
  }

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={collisionDetection}
      accessibility={{
        announcements,
        screenReaderInstructions: {
          draggable:
            'To pick up a card, press space or enter on its drag handle. Use the left and right arrow keys to move it between columns, press space or enter to drop it, or escape to cancel.',
        },
      }}
      onDragStart={({ active }) => setActiveTitle(labelFor(active.id))}
      onDragCancel={() => setActiveTitle(null)}
      onDragEnd={onDragEnd}
    >
      <div className="grid gap-3 overflow-x-auto pb-4 md:grid-cols-5" role="list" aria-label="Application stages" data-dragging={activeTitle !== null}>
        {STAGES.map((stage) => {
          const column = board.columns.find((item) => item.stage === stage)
          return <KanbanColumn key={stage} stage={stage} cards={column?.cards ?? []} />
        })}
      </div>
    </DndContext>
  )
}

function KanbanColumn({ stage, cards }: { stage: Stage; cards: BoardCard[] }) {
  const { setNodeRef, isOver } = useDroppable({ id: columnId(stage), data: { stage } })
  return (
    <section
      ref={setNodeRef}
      role="listitem"
      aria-label={`${STAGE_LABELS[stage]} column`}
      className={`flex min-h-[200px] min-w-[240px] flex-col rounded-lg border border-t-4 border-stone-200 bg-stone-100/70 p-2 dark:border-stone-800 dark:bg-stone-900/60 ${COLUMN_TONES[stage]} ${
        isOver ? 'ring-2 ring-brand-600' : ''
      }`}
    >
      <h2 className="mb-2 flex items-center justify-between px-1 text-sm font-semibold">
        {STAGE_LABELS[stage]}
        <span className="rounded-full bg-stone-200 px-2 text-xs font-medium tabular-nums dark:bg-stone-800" aria-label={`${cards.length} cards`}>
          {cards.length}
        </span>
      </h2>
      <ul className="flex-1 space-y-2">
        {cards.map((card) => (
          <li key={card.application.id}>
            <KanbanCard card={card} />
          </li>
        ))}
      </ul>
      {cards.length === 0 ? <p className="px-1 py-6 text-center text-xs text-stone-400">Drop a card here</p> : null}
    </section>
  )
}

function KanbanCard({ card }: { card: BoardCard }) {
  const { application } = card
  const move = useMoveCard()
  const patch = usePatchApplication()
  const remove = useDeleteApplication()
  const { notify } = useToast()
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, isDragging } = useDraggable({
    id: cardId(application.id),
    data: { stage: application.stage, title: card.title },
  })

  // Local editing state for notes; re-synced when the server value changes (e.g. after a rollback).
  const [notes, setNotes] = useState(application.notes ?? '')
  useEffect(() => setNotes(application.notes ?? ''), [application.notes])

  const overdue = application.stage !== 'rejected' && isOverdue(application.follow_up_at)
  const style = transform ? { transform: `translate3d(${transform.x}px, ${transform.y}px, 0)` } : undefined

  const saveNotes = () => {
    if (notes === (application.notes ?? '')) return
    patch.mutate(
      { id: application.id, patch: { notes: notes.trim() === '' ? null : notes } },
      { onError: (error) => notify(`Could not save notes: ${error.message}`, 'error') },
    )
  }

  const saveFollowUp = (value: string) =>
    patch.mutate(
      { id: application.id, patch: { follow_up_at: value === '' ? null : value } },
      { onError: (error) => notify(`Could not save the follow-up date: ${error.message}`, 'error') },
    )

  return (
    <article
      ref={setNodeRef}
      style={style}
      aria-label={`${card.title} at ${card.company_name}`}
      data-application-id={application.id}
      data-stage={application.stage}
      data-overdue={overdue}
      className={`rounded-lg border bg-white p-3 text-sm shadow-sm dark:bg-stone-900 ${
        overdue ? 'border-rose-400 dark:border-rose-600' : 'border-stone-200 dark:border-stone-700'
      } ${isDragging ? 'relative z-20 opacity-90 shadow-xl' : ''}`}
    >
      <div className="flex items-start gap-2">
        <button
          type="button"
          ref={setActivatorNodeRef}
          {...attributes}
          {...listeners}
          aria-label={`Drag ${card.title}. Space to pick up, arrow keys to change column.`}
          className="mt-0.5 shrink-0 cursor-grab touch-none rounded px-1 text-stone-400 hover:bg-stone-100 hover:text-stone-700 active:cursor-grabbing dark:hover:bg-stone-800"
        >
          ⋮⋮
        </button>
        <div className="min-w-0 flex-1">
          <Link to={`/jobs?job=${application.job_id}`} className="block truncate font-medium hover:underline" title={card.title}>
            {card.title}
          </Link>
          <p className="truncate text-xs text-stone-500">
            {card.company_name || 'Unknown company'}
            {card.location ? ` · ${card.location}` : ''}
          </p>
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1">
        {card.job_status === 'closed' ? <Chip tone="warn">Posting closed</Chip> : null}
        {application.applied_at ? <Chip>Applied {formatDate(application.applied_at)}</Chip> : null}
        {overdue ? <Chip tone="warn">Follow-up overdue</Chip> : null}
      </div>

      <div className="mt-2 grid grid-cols-2 gap-2">
        <label className="text-xs text-stone-500">
          Move to
          <select
            value={application.stage}
            onChange={(event) => move(application.id, event.target.value as Stage, card.title)}
            className="mt-0.5 block w-full rounded border border-stone-300 bg-white px-1.5 py-1 text-sm text-stone-900 dark:border-stone-600 dark:bg-stone-800 dark:text-stone-100"
          >
            {STAGES.map((stage) => (
              <option key={stage} value={stage}>
                {STAGE_LABELS[stage]}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-stone-500">
          Follow up
          <input
            type="date"
            value={application.follow_up_at?.slice(0, 10) ?? ''}
            onChange={(event) => saveFollowUp(event.target.value)}
            className={`mt-0.5 block w-full rounded border bg-white px-1.5 py-0.5 text-sm text-stone-900 dark:bg-stone-800 dark:text-stone-100 ${
              overdue ? 'border-rose-400 font-medium text-rose-700 dark:text-rose-300' : 'border-stone-300 dark:border-stone-600'
            }`}
          />
        </label>
      </div>

      <div className="mt-2 grid grid-cols-3 gap-1.5">
        {(['contact_name', 'contact_email', 'contact_phone'] as const).map((field) => (
          <label key={field} className="text-xs text-stone-500">
            {field === 'contact_name' ? 'Recruiter' : field === 'contact_email' ? 'Email' : 'Phone'}
            <input
              type="text"
              defaultValue={application[field] ?? ''}
              maxLength={200}
              onBlur={(event) => {
                const value = event.target.value.trim()
                if (value === (application[field] ?? '')) return
                patch.mutate(
                  { id: application.id, patch: { [field]: value === '' ? null : value } },
                  { onError: (error) => notify(`Could not save the contact: ${error.message}`, 'error') },
                )
              }}
              className="mt-0.5 block w-full rounded border border-stone-300 bg-white px-1.5 py-0.5 text-sm text-stone-900 dark:border-stone-600 dark:bg-stone-800 dark:text-stone-100"
            />
          </label>
        ))}
      </div>

      <label className="mt-2 block text-xs text-stone-500">
        Notes
        <textarea
          value={notes}
          onChange={(event) => setNotes(event.target.value)}
          onBlur={saveNotes}
          rows={2}
          maxLength={10000}
          placeholder="Recruiter, referral, interview dates…"
          className="mt-0.5 block w-full resize-y rounded border border-stone-300 bg-white px-1.5 py-1 text-sm text-stone-900 dark:border-stone-600 dark:bg-stone-800 dark:text-stone-100"
        />
      </label>

      <div className="mt-2 flex items-center justify-between text-xs">
        {application.tailored_resume_id ? (
          <Link
            to={`/resumes?resume=${application.tailored_resume_id}`}
            className="mr-2 text-emerald-700 hover:underline dark:text-emerald-300"
          >
            Resume {application.resume_number ? `R-${String(application.resume_number).padStart(4, '0')}` : '✓'}
          </Link>
        ) : null}
        {card.apply_url ? (
          <a href={card.apply_url} target="_blank" rel="noopener noreferrer" className="text-brand-700 hover:underline dark:text-brand-100">
            Open apply page ↗
          </a>
        ) : (
          <span className="text-stone-400">No apply link</span>
        )}
        {application.stage === 'saved' ? (
          <button
            type="button"
            onClick={() =>
              remove.mutate(application.id, {
                onError: (error) => notify(`Could not remove “${card.title}”: ${error.message}`, 'error'),
              })
            }
            className="rounded px-1.5 py-0.5 text-rose-700 hover:bg-rose-50 dark:text-rose-300 dark:hover:bg-rose-950"
          >
            Remove
          </button>
        ) : null}
      </div>
    </article>
  )
}
