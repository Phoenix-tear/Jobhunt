import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { useBoard, useHealth, useResumes, useSetupState } from '../api/hooks'
import { DemoBanner } from './DemoBanner'

function useTheme(): [boolean, () => void] {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains('dark'))
  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    try {
      localStorage.setItem('jobhunt-theme', dark ? 'dark' : 'light')
    } catch {
      // private mode / storage disabled: the theme just will not persist
    }
  }, [dark])
  return [dark, () => setDark((value) => !value)]
}

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
    isActive
      ? 'bg-stone-900 text-white dark:bg-stone-100 dark:text-stone-900'
      : 'text-stone-600 hover:bg-stone-200 dark:text-stone-300 dark:hover:bg-stone-800'
  }`

export function AppShell() {
  const [dark, toggleTheme] = useTheme()
  const board = useBoard()
  const health = useHealth()
  const resumes = useResumes()
  const setup = useSetupState()
  const isDemo = Boolean(setup.data?.demo_mode)
  const warningCount = health.data?.warnings.length ?? 0

  return (
    <div className="flex min-h-dvh flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2 focus:text-stone-900">
        Skip to content
      </a>
      {isDemo && <DemoBanner />}
      <header className="sticky top-0 z-30 border-b border-stone-200 bg-stone-50/90 backdrop-blur dark:border-stone-800 dark:bg-stone-950/90">
        <div className="mx-auto flex max-w-[1500px] flex-wrap items-center gap-x-4 gap-y-1.5 px-4 py-2.5">
          <NavLink to="/today" className="flex items-center gap-2 font-semibold tracking-tight">
            <span aria-hidden className="grid size-7 place-items-center rounded-lg bg-brand-700 text-sm font-bold text-white">
              j
            </span>
            jobhunt
          </NavLink>
          <nav aria-label="Main" className="order-last flex w-full items-center gap-1 sm:order-none sm:w-auto">
            <NavLink to="/today" className={linkClass}>
              Today
            </NavLink>
            <NavLink to="/jobs" className={linkClass}>
              Jobs
            </NavLink>
            <NavLink to="/resumes" className={linkClass}>
              Resumes
              {resumes.data && (resumes.data.counts.visible ?? 0) > 0 ? (
                <span className="ml-1.5 rounded-full bg-brand-100 px-1.5 text-xs text-brand-900 dark:bg-brand-900/60 dark:text-brand-100" aria-label={`${resumes.data.counts.visible} resumes`}>
                  {resumes.data.counts.visible}
                </span>
              ) : null}
            </NavLink>
            <NavLink to="/tracker" className={linkClass}>
              Tracker
              {board.data && board.data.total > 0 ? (
                <span className="ml-1.5 rounded-full bg-brand-100 px-1.5 text-xs text-brand-900 dark:bg-brand-900/60 dark:text-brand-100">
                  {board.data.total}
                </span>
              ) : null}
            </NavLink>
            <NavLink to="/health" className={linkClass}>
              Health
              {warningCount > 0 ? (
                <span className="ml-1.5 rounded-full bg-amber-200 px-1.5 text-xs text-amber-950" aria-label={`${warningCount} warnings`}>
                  {warningCount}
                </span>
              ) : null}
            </NavLink>
            <NavLink to="/settings" className={linkClass}>
              Settings
            </NavLink>
          </nav>
          <button
            type="button"
            onClick={toggleTheme}
            className="ml-auto rounded-md border border-stone-300 px-2.5 py-1 text-xs font-medium hover:bg-stone-200 dark:border-stone-700 dark:hover:bg-stone-800"
            aria-pressed={dark}
          >
            {dark ? 'Light mode' : 'Dark mode'}
          </button>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-[1500px] flex-1 px-4 py-5">
        <Outlet />
      </main>
    </div>
  )
}
