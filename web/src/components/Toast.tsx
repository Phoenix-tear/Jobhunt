import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'

type ToastKind = 'error' | 'info'
export interface ToastAction {
  label: string
  onClick: () => void
}

interface ToastItem {
  id: number
  kind: ToastKind
  message: string
  action?: ToastAction
}

interface ToastApi {
  notify: (message: string, kind?: ToastKind, action?: ToastAction) => void
}

const ToastContext = createContext<ToastApi>({ notify: () => undefined })

export function useToast(): ToastApi {
  return useContext(ToastContext)
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const nextId = useRef(1)

  const dismiss = useCallback((id: number) => setItems((current) => current.filter((item) => item.id !== id)), [])
  const notify = useCallback(
    (message: string, kind: ToastKind = 'info', action?: ToastAction) => {
      const id = nextId.current++
      setItems((current) => [...current.slice(-3), { id, kind, message, action }])
      window.setTimeout(() => dismiss(id), 7000)
    },
    [dismiss],
  )
  const api = useMemo(() => ({ notify }), [notify])

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="pointer-events-none fixed inset-x-0 bottom-4 z-50 flex flex-col items-center gap-2 px-4" aria-live="polite">
        {items.map((item) => (
          <div
            key={item.id}
            role={item.kind === 'error' ? 'alert' : 'status'}
            className={`pointer-events-auto flex max-w-xl items-start gap-3 rounded-lg border px-4 py-3 text-sm shadow-lg ${
              item.kind === 'error'
                ? 'border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-100'
                : 'border-stone-300 bg-white text-stone-900 dark:border-stone-700 dark:bg-stone-900 dark:text-stone-100'
            }`}
          >
            <span className="break-words">{item.message}</span>
            {item.action ? (
              <button
                type="button"
                className="ml-auto shrink-0 rounded border border-current px-2 py-0.5 text-xs font-semibold"
                onClick={() => {
                  item.action?.onClick()
                  dismiss(item.id)
                }}
              >
                {item.action.label}
              </button>
            ) : null}
            <button type="button" onClick={() => dismiss(item.id)} className="shrink-0 text-xs font-medium underline" aria-label="Dismiss message">
              Dismiss
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
