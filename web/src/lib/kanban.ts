import type { KeyboardCoordinateGetter } from '@dnd-kit/core'
import type { Stage } from '../api/types'
import { STAGES } from '../api/types'

export const COLUMN_PREFIX = 'column:'
export const CARD_PREFIX = 'card:'

export const columnId = (stage: Stage): string => `${COLUMN_PREFIX}${stage}`
export const cardId = (applicationId: number): string => `${CARD_PREFIX}${applicationId}`

export function stageFromColumnId(id: unknown): Stage | null {
  if (typeof id !== 'string' || !id.startsWith(COLUMN_PREFIX)) return null
  const stage = id.slice(COLUMN_PREFIX.length)
  return (STAGES as readonly string[]).includes(stage) ? (stage as Stage) : null
}

export function applicationIdFromCardId(id: unknown): number | null {
  if (typeof id !== 'string' || !id.startsWith(CARD_PREFIX)) return null
  const value = Number.parseInt(id.slice(CARD_PREFIX.length), 10)
  return Number.isFinite(value) ? value : null
}

/**
 * What should a finished drag do? Returns the move to perform, or null when the card was dropped
 * outside a column, back where it started, or the ids are not ours.
 */
export function resolveDrop(
  activeId: unknown,
  overId: unknown,
  currentStage: Stage,
): { applicationId: number; stage: Stage } | null {
  const applicationId = applicationIdFromCardId(activeId)
  const stage = stageFromColumnId(overId)
  if (applicationId === null || stage === null || stage === currentStage) return null
  return { applicationId, stage }
}

/**
 * Keyboard dragging on a board: dnd-kit's default moves the card 25px per arrow press, which
 * never gets out of a column. Here ArrowLeft/ArrowRight jump the card into the neighboring
 * column (Space picks up and drops, Escape cancels, as in dnd-kit's defaults).
 */
export const columnKeyboardCoordinates: KeyboardCoordinateGetter = (event, { context, currentCoordinates }) => {
  if (event.code !== 'ArrowRight' && event.code !== 'ArrowLeft') return undefined
  const { collisionRect, droppableRects } = context
  if (!collisionRect) return undefined

  const columns = STAGES.map((stage) => ({ stage, rect: droppableRects.get(columnId(stage)) }))
    .filter((column): column is { stage: Stage; rect: NonNullable<typeof column.rect> } => column.rect !== undefined)
    .sort((a, b) => a.rect.left - b.rect.left)
  if (columns.length === 0) return undefined

  const centerX = collisionRect.left + collisionRect.width / 2
  let currentIndex = columns.findIndex((column) => centerX >= column.rect.left && centerX <= column.rect.left + column.rect.width)
  if (currentIndex === -1) {
    currentIndex = columns.reduce(
      (best, column, index) =>
        Math.abs(column.rect.left + column.rect.width / 2 - centerX) <
        Math.abs((columns[best]?.rect.left ?? 0) + (columns[best]?.rect.width ?? 0) / 2 - centerX)
          ? index
          : best,
      0,
    )
  }
  const targetIndex = Math.min(columns.length - 1, Math.max(0, currentIndex + (event.code === 'ArrowRight' ? 1 : -1)))
  const target = columns[targetIndex]
  if (!target || targetIndex === currentIndex) return currentCoordinates

  event.preventDefault()
  return {
    x: target.rect.left + (target.rect.width - collisionRect.width) / 2,
    y: Math.max(target.rect.top + 8, currentCoordinates.y),
  }
}
