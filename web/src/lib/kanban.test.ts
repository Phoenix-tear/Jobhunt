import type { ClientRect } from '@dnd-kit/core'
import { describe, expect, it } from 'vitest'
import { applyPatchToBoard, removeFromBoard } from '../api/hooks'
import { STAGES } from '../api/types'
import { makeCard } from '../test/utils'
import { cardId, columnId, columnKeyboardCoordinates, resolveDrop, stageFromColumnId } from './kanban'

const board = () => ({
  total: 3,
  columns: STAGES.map((stage) => {
    const cards = [makeCard(1, 'saved'), makeCard(2, 'saved'), makeCard(3, 'applied')].filter((card) => card.application.stage === stage)
    return { stage, count: cards.length, cards }
  }),
})

describe('resolveDrop', () => {
  it('moves a card dropped on another column', () => {
    expect(resolveDrop(cardId(7), columnId('offer'), 'applied')).toEqual({ applicationId: 7, stage: 'offer' })
  })

  it('ignores drops back on the same column, outside columns, or with foreign ids', () => {
    expect(resolveDrop(cardId(7), columnId('applied'), 'applied')).toBeNull()
    expect(resolveDrop(cardId(7), undefined, 'applied')).toBeNull()
    expect(resolveDrop(cardId(7), 'something-else', 'applied')).toBeNull()
    expect(resolveDrop('nope', columnId('offer'), 'applied')).toBeNull()
    expect(stageFromColumnId('column:archived')).toBeNull()
  })
})

describe('board transforms', () => {
  it('moves a card to the front of the target column and stamps applied_at', () => {
    const next = applyPatchToBoard(board(), 1, { stage: 'applied' })
    const saved = next.columns.find((column) => column.stage === 'saved')!
    const applied = next.columns.find((column) => column.stage === 'applied')!
    expect(saved.cards.map((card) => card.application.id)).toEqual([2])
    expect(saved.count).toBe(1)
    expect(applied.cards.map((card) => card.application.id)).toEqual([1, 3])
    expect(applied.cards[0]?.application.stage).toBe('applied')
    expect(applied.cards[0]?.application.applied_at).not.toBeNull()
  })

  it('keeps an existing applied_at and applies notes without moving', () => {
    const start = board()
    start.columns[1]!.cards[0]!.application.applied_at = '2026-09-01'
    const same = applyPatchToBoard(start, 3, { notes: 'call Tuesday' })
    const applied = same.columns.find((column) => column.stage === 'applied')!
    expect(applied.cards[0]?.application.notes).toBe('call Tuesday')
    expect(applied.cards[0]?.application.applied_at).toBe('2026-09-01')
  })

  it('returns the board unchanged for an unknown application and removes cards by id', () => {
    const start = board()
    expect(applyPatchToBoard(start, 999, { stage: 'offer' })).toBe(start)
    const removed = removeFromBoard(start, 2)
    expect(removed.total).toBe(2)
    expect(removed.columns.find((column) => column.stage === 'saved')!.count).toBe(1)
  })
})

type Coordinates = { x: number; y: number }

describe('keyboard column navigation', () => {
  const rect = (left: number, top = 0, width = 200, height = 400): ClientRect => ({
    left, top, width, height, right: left + width, bottom: top + height,
  })
  const context = (cardLeft: number) =>
    ({
      collisionRect: rect(cardLeft, 100, 180, 120),
      droppableRects: new Map([
        [columnId('saved'), rect(0)],
        [columnId('applied'), rect(220)],
        [columnId('oa_interview'), rect(440)],
        [columnId('offer'), rect(660)],
        [columnId('rejected'), rect(880)],
      ]),
    }) as never
  const press = (code: string, cardLeft: number): Coordinates | void =>
    columnKeyboardCoordinates({ code, preventDefault: () => undefined } as KeyboardEvent, {
      active: cardId(1),
      currentCoordinates: { x: cardLeft, y: 100 },
      context: context(cardLeft),
    })

  it('ArrowRight jumps into the next column and ArrowLeft back', () => {
    const toApplied = press('ArrowRight', 10)
    expect(toApplied).toEqual({ x: 220 + (200 - 180) / 2, y: 100 })
    const back = press('ArrowLeft', 230)
    expect(back).toEqual({ x: 0 + (200 - 180) / 2, y: 100 })
  })

  it('stays put at the edges and ignores other keys', () => {
    expect(press('ArrowLeft', 10)).toEqual({ x: 10, y: 100 })
    expect(press('ArrowRight', 890)).toEqual({ x: 890, y: 100 })
    expect(press('ArrowDown', 10)).toBeUndefined()
    expect(press('KeyA', 10)).toBeUndefined()
  })
})
